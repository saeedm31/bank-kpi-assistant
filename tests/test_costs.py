"""Tests for token counting, euro prices, the browser ledger and the monthly budget."""

import datetime as dt
import json
import unittest

from bank_assistant import costs, usage

TODAY = dt.date(2026, 10, 22)


class PriceTests(unittest.TestCase):
    def test_anthropic_usage_and_price(self):
        body = {"usage": {"input_tokens": 100, "output_tokens": 200, "cache_read_input_tokens": 2000,
                          "cache_creation_input_tokens": 0}}
        u = costs.from_anthropic(body, "claude-haiku-5-5-20261001")
        self.assertEqual((u.input, u.output, u.cache_read, u.cache_write, u.estimated), (100, 200, 2000, 0, False))
        # dated model names use the base model's price: (100*0.10 + 2000*0.01 + 200*0.50) / 1e6 USD
        usd = (100 * 0.10 + 2000 * 0.01 + 200 * 0.50) / 1e6
        self.assertAlmostEqual(costs.cost_eur(u, usd_to_eur=0.9), usd * 0.9)

    def test_openai_cached_tokens_are_split_out(self):
        body = {"usage": {"prompt_tokens": 2500, "completion_tokens": 80,
                          "prompt_tokens_details": {"cached_tokens": 2048}}}
        u = costs.from_openai(body, "gpt-6-luna")
        self.assertEqual((u.input, u.cache_read, u.output), (452, 2048, 80))

    def test_local_models_are_free_and_unknown_prices_say_so(self):
        u = costs.from_openai({"usage": {"prompt_tokens": 10, "completion_tokens": 5}}, "llama3.1", "compatible",
                              free=costs.is_local("http://localhost:11434/v1"))
        self.assertEqual(costs.cost_eur(u), 0.0)
        self.assertFalse(costs.is_local("https://openrouter.ai/api/v1"))
        unknown = costs.Usage("some-new-model", "openai", input=10, output=10)
        self.assertIsNone(costs.cost_eur(unknown))
        self.assertIsNone(costs.total([unknown])["eur"])
        self.assertEqual(costs.eur(None), "price unknown")

    def test_caching_makes_repeat_calls_cheaper(self):
        cold = costs.Usage("claude-haiku-5-5", "anthropic", input=20, cache_write=2400, output=90)
        warm = costs.Usage("claude-haiku-5-5", "anthropic", input=20, cache_read=2400, output=90)
        self.assertLess(costs.cost_eur(warm), costs.cost_eur(cold) / 2)

    def test_money_format(self):
        self.assertEqual(costs.eur(0), "€0")
        self.assertEqual(costs.eur(0.00004), "< €0.0001")
        self.assertEqual(costs.eur(0.00031), "€0.0003")
        self.assertEqual(costs.eur(0.123), "€0.123")
        self.assertEqual(costs.eur(12.5), "€12.50")


def summary(eur, calls=2, tokens=1000):
    return {"calls": calls, "input": tokens, "output": 100, "cache_read": 0, "cache_write": 0, "eur": eur,
            "estimated": False}


class LedgerTests(unittest.TestCase):
    def test_totals_per_day_month_and_model(self):
        led = usage.empty(TODAY)
        usage.record(led, summary(0.01), "claude-haiku-5-5", "Card spend by branch", TODAY)
        usage.record(led, summary(0.02), "gpt-6-luna", "NPL ratio", TODAY)
        usage.record(led, summary(0.03), "gpt-6-luna", "Deposits", TODAY - dt.timedelta(days=1))
        usage.record_free(led)
        self.assertAlmostEqual(usage.day_row(led, TODAY)[5], 0.03)
        self.assertAlmostEqual(usage.month_row(led, TODAY)[5], 0.06)
        self.assertEqual(usage.month_row(led, TODAY)[0], 6)
        self.assertAlmostEqual(led["models"]["gpt-6-luna"][5], 0.05)
        self.assertEqual(led["asked"], 3)
        self.assertEqual(led["free"], 1)
        self.assertAlmostEqual(usage.avg_cost_per_question(led), 0.02)
        self.assertEqual(led["top"][0], [0.03, "Deposits"])

    def test_new_month_keeps_last_month_total(self):
        led = usage.empty(dt.date(2026, 9, 30))
        usage.record(led, summary(0.5), "m", "q", dt.date(2026, 9, 30))
        self.assertAlmostEqual(usage.month_row(led, TODAY)[5], 0.0)
        self.assertEqual(led["prev"], {"month": "2026-09", "eur": 0.5})
        self.assertAlmostEqual(led["all"][5], 0.5)

    def test_budget_levels(self):
        led = usage.empty(TODAY, budget=1.0)
        self.assertEqual(usage.status(led, TODAY)["level"], "ok")
        usage.record(led, summary(0.85), "m", "q", TODAY)
        self.assertEqual(usage.status(led, TODAY)["level"], "warn")
        usage.record(led, summary(0.20), "m", "q", TODAY)
        self.assertEqual(usage.status(led, TODAY)["level"], "over")
        led["budget"] = 0
        self.assertEqual(usage.status(led, TODAY)["level"], "ok")      # 0 = no limit

    def test_saved_text_round_trip_and_size(self):
        led = usage.empty(TODAY)
        for d in range(1, 23):
            day = dt.date(2026, 10, d)
            for m in range(8):
                usage.record(led, summary(0.001 * m), f"model-{m}", "a long question " * 10, day)
        text = usage.encode(led)
        self.assertLessEqual(len(text), usage.MAX_BYTES)
        self.assertRegex(text, r"^[A-Za-z0-9_-]+$")                     # plain text, safe to store anywhere
        back = usage.decode(text, TODAY)
        self.assertEqual(back["since"], led["since"])
        self.assertAlmostEqual(back["all"][5], led["all"][5])

    def test_usage_before_loading_is_merged(self):
        saved = usage.empty(TODAY)
        usage.record(saved, summary(0.02), "gpt-6-luna", "NPL ratio", TODAY)
        early = usage.empty(TODAY)
        usage.record(early, summary(0.01), "gpt-6-luna", "Deposits", TODAY)
        usage.record_free(early)
        led = usage.merge(usage.decode(usage.encode(saved), TODAY), early, TODAY)
        self.assertAlmostEqual(usage.month_row(led, TODAY)[5], 0.03)
        self.assertEqual((led["asked"], led["free"], led["models"]["gpt-6-luna"][0]), (2, 1, 4))
        self.assertEqual([t[1] for t in led["top"]], ["NPL ratio", "Deposits"])

    def test_damaged_saved_text_starts_fresh(self):
        for bad in ("", "not base64!", "eyJ2IjoyfQ"):
            led = usage.decode(bad, TODAY)
            self.assertEqual(led["all"], [0, 0, 0, 0, 0, 0.0])
        self.assertNotIn("key", json.dumps(usage.empty(TODAY)))


if __name__ == "__main__":
    unittest.main()
