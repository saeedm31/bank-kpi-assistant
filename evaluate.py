"""Score the assistant on test questions with hand-written correct SQL (eval/questions.yml).

Usage:
    python evaluate.py                                  # built-in engine (no key needed)
    python evaluate.py --ai openai [--model gpt-5-mini]  # AI mode, needs OPENAI_API_KEY
    python evaluate.py --ai anthropic                   # AI mode, needs ANTHROPIC_API_KEY

The data is simulated for a fixed clock (see `now` in the questions file), so results are repeatable.
A question counts as correct when the numbers in the answer's result table match the gold result.
Out-of-scope questions count as correct when the assistant declines instead of guessing.
Writes reports/eval_<mode>_<set>.json.
"""

import argparse
import datetime as dt
import json
import math
import os
import time

import pandas as pd
import yaml

from bank_assistant import guard
from bank_assistant.db import Bank
from bank_assistant.engine import Engine

HERE = os.path.dirname(os.path.abspath(__file__))


def numbers(df):
    """All numeric cells of a result, rounded, sorted."""
    if df is None or df.empty:
        return []
    out = []
    for col in df.columns:
        if not pd.api.types.is_numeric_dtype(df[col]):
            continue
        out += [round(float(v), 4) for v in df[col] if v is not None and not math.isnan(float(v))]
    return sorted(out)


def same(a, b, rel=1e-6, abs_=0.011):
    return len(a) == len(b) and all(math.isclose(x, y, rel_tol=rel, abs_tol=abs_) for x, y in zip(a, b))


def contains(found, gold, rel=1e-3, abs_=0.011):
    """AI answers may add columns; every gold number must appear."""
    pool = list(found)
    for g in gold:
        hit = next((i for i, v in enumerate(pool) if math.isclose(v, g, rel_tol=rel, abs_tol=abs_)), None)
        if hit is None:
            return False
        pool.pop(hit)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ai", choices=["openai", "anthropic"], help="score AI mode instead of the built-in engine")
    ap.add_argument("--model")
    ap.add_argument("--set", default="questions", choices=["questions", "holdout", "holdout2"],
                    help="questions = the main set; holdout = unseen questions not used for tuning")
    args = ap.parse_args()

    with open(os.path.join(HERE, "eval", f"{args.set}.yml"), encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    now = dt.datetime.fromisoformat(spec["now"])
    bank = Bank(now)
    conn = bank.connect()
    engine = Engine(conn)

    llm = None
    if args.ai:
        from bank_assistant import ai
        provider = {"openai": "OpenAI", "anthropic": "Anthropic (Claude)"}[args.ai]
        key = os.environ.get("OPENAI_API_KEY" if args.ai == "openai" else "ANTHROPIC_API_KEY")
        if not key:
            raise SystemExit("Set the API key in the environment first.")
        llm = ai.LLM(provider, key, args.model)

    rows = []
    for item in spec["in_scope"]:
        gold, _ = guard.run(conn, item["gold"])
        gold_n = numbers(gold)
        t = time.time()
        sql, ok, note = "", False, ""
        try:
            if llm:
                ans = ai.ask(llm, item["q"], conn, now)
                sql, found = ans.sql, numbers(ans.data)
                ok = ans.data is not None and len(ans.data) == len(gold) and contains(found, gold_n)
            else:
                q, early = engine.parse(item["q"], now)
                if early:
                    note = "declined"
                else:
                    sql, _ = engine.full_sql(q, now)
                    found = numbers(guard.run(conn, sql)[0])
                    ok = same(found, gold_n)
        except Exception as e:  # noqa: BLE001 - recorded as a failure
            note = f"error: {e}"
        rows.append({"question": item["q"], "kind": "in scope", "correct": bool(ok), "note": note,
                     "seconds": round(time.time() - t, 2), "sql": sql})
        print(("PASS " if ok else "FAIL ") + item["q"] + (f"  ({note})" if note else ""))

    for question in spec["out_of_scope"]:
        if llm:
            ans = ai.ask(llm, question, conn, now)
            declined = not ans.ok or ans.data is None or ans.data.empty
        else:
            q, early = engine.parse(question, now)
            declined = early is not None
        rows.append({"question": question, "kind": "out of scope", "correct": bool(declined), "note": "",
                     "seconds": 0, "sql": ""})
        print(("PASS " if declined else "FAIL ") + question + "  (should decline)")

    df = pd.DataFrame(rows)
    ins, oos = df[df["kind"] == "in scope"], df[df["kind"] == "out of scope"]
    summary = {"mode": f"ai:{args.ai}:{llm.model}" if llm else "built-in", "set": args.set, "clock": spec["now"],
               "in_scope": {"questions": len(ins), "correct": int(ins["correct"].sum()),
                            "declined": int((ins["note"] == "declined").sum())},
               "out_of_scope": {"questions": len(oos), "declined": int(oos["correct"].sum())},
               "wrong_answers": int((~ins["correct"] & (ins["note"] != "declined")).sum()
                                    + (~oos["correct"]).sum())}
    print(json.dumps(summary, indent=2))
    os.makedirs(os.path.join(HERE, "reports"), exist_ok=True)
    name = f"eval_{args.ai or 'builtin'}_{args.set}.json"
    with open(os.path.join(HERE, "reports", name), "w") as f:
        json.dump({"summary": summary, "questions": rows}, f, indent=1)


if __name__ == "__main__":
    main()
