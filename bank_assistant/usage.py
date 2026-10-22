"""The usage ledger kept in the visitor's browser: totals per day, per model, since the last reset.

It is small on purpose (it lives in a cookie, max ~4 KB): daily totals for the current month,
one total for last month, per-model totals, the 5 most expensive questions and the monthly budget.
The API key is never part of it.
"""

import base64
import datetime as dt
import json

COOKIE = "bka_usage"
MAX_BYTES = 3500
WARN_AT = 0.8
DEFAULT_BUDGET = 2.0      # euros per month

# a "row" is [calls, input, output, cache_read, cache_write, eur]


def _row():
    return [0, 0, 0, 0, 0, 0.0]


def _add(row, calls, s):
    row[0] += calls
    row[1] += s["input"]
    row[2] += s["output"]
    row[3] += s["cache_read"]
    row[4] += s["cache_write"]
    row[5] = round(row[5] + (s["eur"] or 0.0), 6)
    return row


def empty(today, budget=DEFAULT_BUDGET):
    return {"v": 1, "since": today.isoformat(), "month": today.strftime("%Y-%m"), "days": {}, "prev": None,
            "models": {}, "all": _row(), "asked": 0, "free": 0, "top": [], "budget": budget}


def roll(state, today):
    """New month: keep last month's total, start the daily list again."""
    month = today.strftime("%Y-%m")
    if state["month"] != month:
        spent = sum(r[5] for r in state["days"].values())
        state["prev"] = {"month": state["month"], "eur": round(spent, 6)}
        state["days"] = {}
        state["month"] = month
    return state


def record(state, summary, model, question, today):
    """Add one AI-answered question (summary from costs.total)."""
    roll(state, today)
    day = today.isoformat()[8:10]
    _add(state["days"].setdefault(day, _row()), summary["calls"], summary)
    _add(state["models"].setdefault(model, _row()), summary["calls"], summary)
    _add(state["all"], summary["calls"], summary)
    state["asked"] = state.get("asked", 0) + 1
    if summary["eur"]:
        top = state["top"] + [[round(summary["eur"], 6), (question or "")[:60]]]
        state["top"] = sorted(top, key=lambda t: -t[0])[:5]
    return state


def record_free(state):
    """A question answered without AI (built-in engine or saved example)."""
    state["free"] += 1
    return state


def month_row(state, today):
    roll(state, today)
    row = _row()
    for r in state["days"].values():
        for i in range(6):
            row[i] += r[i]
    return row


def day_row(state, today):
    roll(state, today)
    return state["days"].get(today.isoformat()[8:10], _row())


def status(state, today):
    """How much of the monthly budget is used: level is ok, warn or over."""
    spent = month_row(state, today)[5]
    budget = state.get("budget") or 0
    share = spent / budget if budget > 0 else 0.0
    level = "over" if budget > 0 and spent >= budget else "warn" if share >= WARN_AT else "ok"
    return {"spent": spent, "budget": budget, "share": share, "level": level}


def avg_cost_per_question(state):
    """Average euros per AI-answered question since the last reset (a question can need several calls)."""
    asked = state.get("asked", 0)
    return state["all"][5] / asked if asked else None


def encode(state):
    """Cookie-safe text (URL-safe base64 of compact JSON, no padding), trimmed to fit."""
    s = dict(state)
    while True:
        raw = base64.urlsafe_b64encode(json.dumps(s, separators=(",", ":")).encode()).decode().rstrip("=")
        if len(raw) <= MAX_BYTES:
            return raw
        if s["top"]:
            s = dict(s, top=s["top"][:-1])
        elif len(s["models"]) > 3:
            keep = sorted(s["models"].items(), key=lambda kv: -kv[1][5])[:3]
            s = dict(s, models=dict(keep))
        else:
            oldest = sorted(s["days"])[0]
            s = dict(s, days={k: v for k, v in s["days"].items() if k != oldest})


def decode(text, today):
    """The ledger from the cookie, or a fresh one if it is missing or damaged."""
    try:
        text = text.strip().strip('"')
        s = json.loads(base64.urlsafe_b64decode((text + "=" * (-len(text) % 4)).encode()).decode())
        if s.get("v") != 1 or not isinstance(s.get("days"), dict) or len(s.get("all", [])) != 6:
            raise ValueError
        s.setdefault("asked", 0)
        return roll(s, today)
    except Exception:
        return empty(today)
