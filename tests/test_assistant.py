"""Tests for the simulator, the SQL guard, the built-in engine, alerts, saved examples and AI mode."""

import datetime as dt
import json
import os
import sqlite3
import tempfile
import unittest

import yaml

from bank_assistant import ai, alerts, examples, guard
from bank_assistant.db import Bank
from bank_assistant.engine import Engine
from bank_assistant.timeparse import find_period

NOW = dt.datetime(2026, 10, 21, 14, 40)
HERE = os.path.dirname(os.path.abspath(__file__))
BANK = CONN = ENGINE = None


def setUpModule():
    global BANK, CONN, ENGINE
    BANK = Bank(NOW, path=os.path.join(tempfile.mkdtemp(), "bank.sqlite"))
    CONN = BANK.connect()
    ENGINE = Engine(CONN)


def one(sql):
    return guard.run(CONN, sql)[0].iloc[0, 0]


class SimulatorTests(unittest.TestCase):
    def test_tables_filled_and_nothing_after_now(self):
        self.assertEqual(one("SELECT COUNT(*) FROM customers"), 2000)
        self.assertGreater(one("SELECT COUNT(*) FROM transactions"), 500_000)
        self.assertLessEqual(one("SELECT MAX(ts) FROM transactions"), NOW.strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(one("SELECT MAX(snapshot_date) FROM balance_snapshots"), "2026-10-21")

    def test_same_clock_same_data(self):
        again = Bank(NOW, path=os.path.join(tempfile.mkdtemp(), "b.sqlite"))
        c = again.connect()
        sql = "SELECT COUNT(*), ROUND(SUM(amount), 2) FROM transactions"
        self.assertEqual(guard.run(c, sql)[0].values.tolist(), guard.run(CONN, sql)[0].values.tolist())

    def test_catch_up_adds_only_payments_that_have_happened(self):
        b = Bank(NOW, path=os.path.join(tempfile.mkdtemp(), "b.sqlite"))
        c = b.connect()
        before = guard.run(c, "SELECT COUNT(*) FROM transactions")[0].iloc[0, 0]
        later = NOW + dt.timedelta(hours=2)
        added = b.catch_up(later)
        after = guard.run(c, "SELECT COUNT(*) FROM transactions")[0].iloc[0, 0]
        self.assertGreater(added, 0)
        self.assertEqual(after - before, added)
        self.assertLessEqual(guard.run(c, "SELECT MAX(ts) FROM transactions")[0].iloc[0, 0],
                             later.strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(b.catch_up(later), 0)

    def test_patterns_exist(self):
        app_share = guard.run(CONN, """SELECT substr(ts, 1, 4) y, AVG(channel = 'App') s FROM transactions
                                      WHERE type = 'Transfer out' GROUP BY y ORDER BY y""")[0]["s"].tolist()
        self.assertGreater(app_share[-1], app_share[0])                   # moving to the app
        june = one("SELECT SUM(amount) FROM transactions WHERE type = 'Salary' AND ts LIKE '2026-06%'")
        may = one("SELECT SUM(amount) FROM transactions WHERE type = 'Salary' AND ts LIKE '2026-05%'")
        self.assertGreater(june / may, 1.8)                                # 14 salaries a year


class GuardTests(unittest.TestCase):
    def test_blocks_anything_but_reading(self):
        for sql in ["DROP TABLE customers", "DELETE FROM loans", "UPDATE loans SET status = 'Current'",
                    "INSERT INTO branches VALUES (9, 'x', 'y', 'z')", "ATTACH DATABASE 'x.db' AS x",
                    "PRAGMA table_info(customers)", "SELECT 1; DROP TABLE customers", "SELECT * FROM sqlite_master",
                    "WITH x AS (SELECT 1) DELETE FROM customers", "SELECT load_extension('x')"]:
            with self.subTest(sql=sql), self.assertRaises((guard.UnsafeQuery, sqlite3.Error)):
                guard.run(CONN, sql)
        self.assertEqual(one("SELECT COUNT(*) FROM customers"), 2000)

    def test_time_limit_and_row_cap(self):
        with self.assertRaises(guard.UnsafeQuery):
            guard.run(CONN, "WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM r) SELECT COUNT(*) FROM r",
                      time_limit=1)
        df, truncated = guard.run(CONN, "SELECT * FROM transactions", max_rows=50)
        self.assertEqual(len(df), 50)
        self.assertTrue(truncated)

    def test_cleans_model_output(self):
        self.assertEqual(guard.clean("```sql\nSELECT 1;\n```"), "SELECT 1")
        self.assertEqual(len(guard.run(CONN, "select ';' as x")[0]), 1)


class TimeTests(unittest.TestCase):
    def period(self, text):
        return find_period(f" {text} ", NOW)[0]

    def test_periods(self):
        p = self.period("last month")
        self.assertEqual((p.start, p.end), (dt.datetime(2026, 9, 1), dt.datetime(2026, 10, 1)))
        p = self.period("this week")
        self.assertEqual((p.start, p.end), (dt.datetime(2026, 10, 19), NOW))
        p = self.period("in q2 2025")
        self.assertEqual((p.start, p.end), (dt.datetime(2025, 4, 1), dt.datetime(2025, 7, 1)))
        p = self.period("since march")
        self.assertEqual(p.start, dt.datetime(2026, 3, 1))
        p = self.period("this summer")
        self.assertEqual((p.start, p.end), (dt.datetime(2026, 6, 1), dt.datetime(2026, 9, 1)))
        p = self.period("last 24 hours")
        self.assertEqual(p.start, NOW - dt.timedelta(hours=24))
        self.assertIsNone(self.period("may i see card spend"))

    def test_first_period_in_sentence_wins(self):
        p = find_period(" card spend this month compared with last month ", NOW)[0]
        self.assertEqual(p.start, dt.datetime(2026, 10, 1))


class EngineTests(unittest.TestCase):
    def ask(self, question, previous=None):
        q, early = ENGINE.parse(question, NOW, previous)
        return q, early

    def test_reads_question(self):
        q, _ = self.ask("Card spend of premium customers in Lisbon by branch last month vs the month before")
        self.assertEqual(q.metric, "card_spend")
        self.assertEqual(q.filters, {"segment": ["Premium"], "city": ["Lisboa"]})
        self.assertEqual(q.group, ["branch"])
        self.assertEqual(q.period.start, dt.datetime(2026, 9, 1))

    def test_count_words_switch_kpi(self):
        self.assertEqual(self.ask("How many mortgages did we issue in 2025?")[0].metric, "loans_issued_count")
        self.assertEqual(self.ask("How many card payments today?")[0].metric, "card_payments")

    def test_numbers_match_hand_written_sql(self):
        q, _ = self.ask("Card spend by branch last month")
        df, _ = guard.run(CONN, ENGINE.full_sql(q, NOW)[0])
        gold = guard.run(CONN, """SELECT b.name, SUM(t.amount) v FROM transactions t
            JOIN customers c ON c.customer_id = t.customer_id JOIN branches b ON b.branch_id = c.branch_id
            WHERE t.type = 'Card payment' AND t.ts >= '2026-09-01' AND t.ts < '2026-10-01' GROUP BY b.name""")[0]
        self.assertEqual(sorted(df["card_spend"].round(2)), sorted(gold["v"].round(2)))

    def test_follow_up_keeps_the_kpi(self):
        q1, _ = self.ask("Card spend last month")
        q2, _ = self.ask("and in Faro?", q1)
        self.assertEqual(q2.metric, "card_spend")
        self.assertEqual(q2.filters, {"city": ["Faro"]})
        self.assertEqual(q2.period, q1.period)

    def test_declines_instead_of_guessing(self):
        for question in ["Which customers are likely to leave?", "Forecast deposits for next quarter",
                         "What's the weather in Porto?", "Delete all closed accounts",
                         "What percentage of transfers were made at a branch last month?"]:
            with self.subTest(question=question):
                q, early = self.ask(question)
                self.assertIsNone(q)
                self.assertFalse(early.ok)

    def test_answer_text(self):
        a, _ = ENGINE.answer("NPL ratio vs last month", CONN, NOW)
        self.assertIn("NPL ratio today is", a.text)
        self.assertIn("30 September 2026", a.text)
        self.assertTrue(a.understood)

    def test_main_evaluation_set_passes(self):
        with open(os.path.join(HERE, "..", "eval", "questions.yml")) as f:
            spec = yaml.safe_load(f)
        self.assertEqual(spec["now"], NOW.strftime("%Y-%m-%d %H:%M:%S"))
        from evaluate import numbers, same
        failed = []
        for item in spec["in_scope"]:
            q, early = ENGINE.parse(item["q"], NOW)
            if early:
                failed.append(item["q"])
                continue
            got = numbers(guard.run(CONN, ENGINE.full_sql(q, NOW)[0])[0])
            if not same(got, numbers(guard.run(CONN, item["gold"])[0])):
                failed.append(item["q"])
        self.assertEqual(failed, [])
        self.assertTrue(all(ENGINE.parse(q, NOW)[1] is not None for q in spec["out_of_scope"]))


class AlertTests(unittest.TestCase):
    def test_finds_the_planted_events(self):
        found = alerts.find(CONN, NOW)
        titles = " | ".join(a["title"] for a in found)
        for e in BANK.events:
            self.assertIn(e["branch"], titles)

    def test_quiet_days_have_no_alerts(self):
        for days in (5, 12, 19, 33):
            self.assertEqual(alerts.find(CONN, NOW - dt.timedelta(days=days)), [])


class ExampleTests(unittest.TestCase):
    def test_saved_examples_run(self):
        for e in examples.EXAMPLES:
            with self.subTest(example=e["id"]):
                a = examples.run(e, CONN, NOW)
                self.assertFalse(a.data.empty)
                self.assertNotIn("Could not summarise", a.text)


class FakeLLM:
    """Replies from a script, like a model would."""
    model = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def complete(self, system, messages):
        self.seen.append(messages[-1]["content"])
        return self.replies.pop(0)


class AITests(unittest.TestCase):
    def test_retries_after_a_failed_query_and_refused_sql(self):
        llm = FakeLLM([json.dumps({"sql": "DROP TABLE customers"}),
                       "```json\n" + json.dumps({"sql": "SELECT SUM(amount_eur) FROM transactions"}) + "\n```",
                       json.dumps({"sql": "SELECT COUNT(*) AS n FROM customers"}),
                       "We have 2,000 customers."])
        a = ai.ask(llm, "how many customers?", CONN, NOW)
        self.assertEqual(a.text, "We have 2,000 customers.")
        self.assertEqual(a.data.iloc[0, 0], 2000)
        self.assertEqual(len(a.understood), 3)       # two failed attempts + the model name
        self.assertIn("failed with", llm.seen[1])
        self.assertEqual(one("SELECT COUNT(*) FROM customers"), 2000)

    def test_clarifying_question(self):
        a = ai.ask(FakeLLM([json.dumps({"clarify": "Which year?"})]), "card spend", CONN, NOW)
        self.assertFalse(a.ok)
        self.assertEqual(a.text, "Which year?")

    def test_prompt_has_schema_and_kpis(self):
        self.assertIn("TABLE loan_snapshots", ai.schema_text())
        self.assertIn("NPL ratio", ai.kpi_text())


if __name__ == "__main__":
    unittest.main()
