"""Alerts: find what is unusual today and yesterday, before anyone asks.

For each branch, compare card payments, ATM withdrawals and transfers out with the
same weekday and the same hours in the previous four weeks. Flag a change only when
it is both large and very unlikely to be chance:
- counts (card payments, ATM withdrawals): at least 40% away from usual, and a
  chance of less than 1 in a million under a Poisson model of the usual rate;
- money (transfers out): at least double (or half) the usual, 4+ standard deviations of
  the normal week-to-week swing (floored at 50% of usual, as single transfers can be
  large), and at least €100k away from usual.
"""

import datetime as dt
import math

import numpy as np

from . import guard
from .engine import fmt

CHECKS = [  # id, label, filter, unit, question words
    ("card_count", "Card payments", "t.type = 'Card payment'", "count", "card payments"),
    ("atm_count", "ATM withdrawals", "t.type = 'ATM withdrawal'", "count", "ATM withdrawals"),
    ("transfer_eur", "Transfers out", "t.type = 'Transfer out'", "eur", "transfers out"),
]
WEEKS = 4
MIN_EVENTS = 5
MIN_EUR = 100000      # money alerts need at least €100k away from usual
MIN_Z = 4.0
MAX_P = 1e-6          # count alerts: chance under the usual rate


def poisson_tail(k, mean):
    """Chance of a count at least this far from `mean` (one-sided, in the direction of k)."""
    k = int(round(k))
    logs = [i * math.log(mean) - mean - math.lgamma(i + 1) for i in range(0, max(k, int(mean * 4) + 50) + 1)]
    probs = np.exp(logs)
    return float(probs[k:].sum() if k >= mean else probs[:k + 1].sum())


def _windows(now):
    today = dt.datetime(now.year, now.month, now.day)
    out = []
    if now - today >= dt.timedelta(hours=2):
        out.append(("today", today, now))
    out.append(("yesterday", today - dt.timedelta(days=1), today))
    return out


def sql_for(now):
    cases, ranges = [], []
    for name, start, end in _windows(now):
        for k in range(WEEKS + 1):
            s, e = start - dt.timedelta(weeks=k), end - dt.timedelta(weeks=k)
            cond = f"t.ts >= '{s:%Y-%m-%d %H:%M:%S}' AND t.ts < '{e:%Y-%m-%d %H:%M:%S}'"
            cases.append(f"WHEN {cond} THEN '{name}|{k}'")
            ranges.append(f"({cond})")
    return ("SELECT CASE " + " ".join(cases) + " END AS win, b.name AS branch, t.type,\n"
            "       COUNT(*) AS n, SUM(t.amount) AS eur\n"
            "FROM transactions t\nJOIN customers c ON c.customer_id = t.customer_id\n"
            "JOIN branches b ON b.branch_id = c.branch_id\n"
            "WHERE t.type IN ('Card payment', 'ATM withdrawal', 'Transfer out')\n  AND (" + "\n    OR ".join(ranges) + ")\n"
            "GROUP BY win, b.name, t.type")


TYPE_OF = {"card_count": "Card payment", "atm_count": "ATM withdrawal", "transfer_eur": "Transfer out"}


def find(conn, now):
    df, _ = guard.run(conn, sql_for(now), time_limit=20)
    if df.empty:
        return []
    df[["window", "k"]] = df["win"].str.split("|", expand=True)
    df["k"] = df["k"].astype(int)
    branches = sorted(df["branch"].unique())
    alerts = []
    for name, start, end in _windows(now):
        for cid, label, _, unit, words in CHECKS:
            sub = df[(df["window"] == name) & (df["type"] == TYPE_OF[cid])]
            for br in branches:
                rows = sub[sub["branch"] == br].set_index("k")
                vals = np.array([float(rows.loc[k, "n" if unit == "count" else "eur"]) if k in rows.index else 0.0
                                 for k in range(WEEKS + 1)])
                counts = np.array([float(rows.loc[k, "n"]) if k in rows.index else 0.0 for k in range(WEEKS + 1)])
                cur, base = vals[0], vals[1:]
                mean = base.mean()
                if mean <= 0 or (unit == "count" and counts[1:].mean() < MIN_EVENTS):
                    continue
                if unit == "count":
                    p = poisson_tail(cur, mean)
                    z = (cur - mean) / np.sqrt(mean) if p < MAX_P else 0.0
                    min_change = .4
                else:
                    z = (cur - mean) / max(base.std(ddof=1), .5 * mean)
                    min_change = 1.0
                change = cur / mean - 1
                big_enough = unit == "count" or abs(cur - mean) >= MIN_EUR
                if abs(change) >= min_change and abs(z) >= MIN_Z and big_enough:
                    span = "today so far" if name == "today" else "yesterday"
                    alerts.append({
                        "when": name, "branch": br, "metric": label, "unit": unit, "current": cur, "usual": mean,
                        "change": change, "z": z, "severity": "high" if abs(z) >= 6 else "medium",
                        "title": f"{label} {'up' if change > 0 else 'down'} {abs(change):.0%} — {br}, {span}",
                        "text": (f"{label} by {br} customers {span}: **{fmt(cur, unit)}**, against "
                                 f"{fmt(mean, unit)} on the same {start:%A}{' up to ' + end.strftime('%H:%M') if name == 'today' else ''} "
                                 f"in the last {WEEKS} weeks."),
                        "question": f"{words} by hour {name} in {br}",
                    })
    # one alert per branch and window: keep the strongest
    alerts.sort(key=lambda a: -abs(a["z"]))
    seen, out = set(), []
    for a in alerts:
        key = (a["branch"], a["when"], a["metric"])
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out
