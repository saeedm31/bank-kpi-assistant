"""Saved examples: harder questions with SQL written and checked by hand. They run live on today's data.

These are the kind of questions the built-in engine does not write itself (several tables, customer-level
logic, what-if). They show what AI mode is for, and they work without an API key.
"""

import datetime as dt

from .engine import Answer, fmt


def _dates(now):
    d = lambda days: (now - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return {"now": now.strftime("%Y-%m-%d %H:%M:%S"), "today": now.date().isoformat(), "d30": d(30), "d45": d(45),
            "d90": d(90), "d180": d(180), "d365": d(365), "last_year": str(now.year - 1),
            "this_year": str(now.year)}


def _salary_stopped(df, now):
    if df.empty:
        return "No current customer has missed their usual salary."
    seg = df["segment"].value_counts()
    top_branch = df["branch"].value_counts()
    return (f"**{len(df)} customers** used to get their salary here but none has arrived for over 45 days. "
            f"This is a common sign a customer is moving to another bank. Most are {seg.index[0]} customers "
            f"({seg.iloc[0]}), and {top_branch.index[0]} has the most ({top_branch.iloc[0]}). "
            f"Worth a call from their branch.")


def _cross_sell(df, now):
    r = df.iloc[0]
    return (f"Of **{int(r['mortgage_customers'])}** customers with a live mortgage, **{int(r['with_savings'])}** "
            f"({r['share_pct']:.0f}%) also have a savings account. The other {int(r['mortgage_customers'] - r['with_savings'])} "
            f"are a ready list for a savings offer.")


def _app_spend(df, now):
    piv = df.pivot(index="age_band", columns="group", values="card_spend_per_customer")
    overall = df.groupby("group").apply(lambda g: (g["card_spend_per_customer"] * g["customers"]).sum() / g["customers"].sum(),
                                        include_groups=False)
    gap_all = 100 * (overall["App users"] / overall["Not app users"] - 1)
    gaps = 100 * (piv["App users"] / piv["Not app users"] - 1)
    more = f"{abs(gap_all):.0f}% {'more' if gap_all >= 0 else 'less'}"
    return (f"Overall, app users spend **{more}** on cards than other retail customers "
            f"(last 90 days). But inside each age band the gap is small ({gaps.min():+.0f}% to {gaps.max():+.0f}%): "
            f"app users are simply younger. The app doesn't seem to make people spend more.")


def _top_depositors(df, now):
    total = df["total_balance"].sum()
    return (f"The 10 largest depositors hold **{fmt(total, 'eur')}** together. "
            f"{(df['segment'] == 'Premium').sum()} of them are Premium and {(df['loans'] > 0).sum()} also have a loan "
            f"with us. Top: customer {df.iloc[0]['customer_id']} ({df.iloc[0]['branch']}, {fmt(df.iloc[0]['total_balance'], 'eur')}).")


def _cohort(df, now):
    joined, still, active = df["joined"].sum(), df["still_customers"].sum(), df["active_last_30_days"].sum()
    worst = df.assign(r=df["active_last_30_days"] / df["joined"]).sort_values("r").iloc[0]
    return (f"Of **{joined}** customers who joined in {now.year - 1}, **{still}** ({100 * still / joined:.0f}%) are still "
            f"customers and **{active}** ({100 * active / joined:.0f}%) made a payment themselves in the last 30 days. "
            f"The weakest month is {worst['joined_month']} ({100 * worst['r']:.0f}% active).")


def _fee_waiver(df, now):
    r = df.iloc[0]
    return (f"Waiving account fees for customers aged 18-24 would cost about **{fmt(r['fees_18_24'], 'eur')}** a year "
            f"({r['share_pct']:.1f}% of all fee income), based on the last 12 months, for "
            f"{int(r['customers_18_24'])} customers.")


EXAMPLES = [
    {"id": "salary_stopped",
     "question": "Which customers stopped receiving their salary here?",
     "why": "Early warning of customers about to leave: salary used to arrive monthly, but not in the last 45 days.",
     "sql": """WITH salary AS (
  SELECT customer_id, MAX(ts) AS last_salary, COUNT(*) AS salaries_6m
  FROM transactions
  WHERE type = 'Salary' AND ts >= '{d180}'
  GROUP BY customer_id
)
SELECT c.customer_id, c.segment, c.age_band, b.name AS branch,
       substr(s.last_salary, 1, 10) AS last_salary, s.salaries_6m
FROM salary s
JOIN customers c ON c.customer_id = s.customer_id
JOIN branches b ON b.branch_id = c.branch_id
WHERE c.closed_date IS NULL
  AND s.last_salary < '{d45}'
  AND s.salaries_6m >= 2
ORDER BY s.last_salary""",
     "answer": _salary_stopped},
    {"id": "cross_sell",
     "question": "What share of mortgage customers also have a savings account?",
     "why": "Cross-selling: customers with a home loan but no savings account.",
     "sql": """SELECT COUNT(DISTINCT l.customer_id) AS mortgage_customers,
       COUNT(DISTINCT CASE WHEN a.account_id IS NOT NULL THEN l.customer_id END) AS with_savings,
       ROUND(100.0 * COUNT(DISTINCT CASE WHEN a.account_id IS NOT NULL THEN l.customer_id END)
             / COUNT(DISTINCT l.customer_id), 1) AS share_pct
FROM loans l
JOIN customers c ON c.customer_id = l.customer_id AND c.closed_date IS NULL
LEFT JOIN accounts a ON a.customer_id = l.customer_id
      AND a.product = 'Savings account' AND a.closed_date IS NULL
WHERE l.product = 'Mortgage' AND l.status <> 'Repaid'""",
     "answer": _cross_sell},
    {"id": "app_spend",
     "question": "Do app users spend more on cards than customers who don't use the app?",
     "why": "A fair comparison: same segment (retail), same 90 days, and split by age so age doesn't fool us.",
     "sql": """SELECT c.age_band,
       CASE WHEN c.app_user_since <= '{d90}' THEN 'App users' ELSE 'Not app users' END AS "group",
       COUNT(*) AS customers,
       ROUND(SUM(COALESCE(t.spend, 0)) / COUNT(*), 2) AS card_spend_per_customer
FROM customers c
LEFT JOIN (SELECT customer_id, SUM(amount) AS spend FROM transactions
           WHERE type = 'Card payment' AND ts >= '{d90}'
           GROUP BY customer_id) t ON t.customer_id = c.customer_id
WHERE c.segment = 'Retail' AND c.closed_date IS NULL AND c.joined_date <= '{d90}'
GROUP BY c.age_band, "group"
ORDER BY c.age_band, "group\"""",
     "answer": _app_spend},
    {"id": "top_depositors",
     "question": "Who are our 10 largest depositors, and what else do they hold?",
     "why": "Customer-level view joining today's balances, accounts and loans.",
     "sql": """SELECT c.customer_id, c.segment, b.name AS branch,
       ROUND(SUM(a.balance), 2) AS total_balance,
       COUNT(*) AS accounts,
       (SELECT COUNT(*) FROM loans l WHERE l.customer_id = c.customer_id AND l.status <> 'Repaid') AS loans
FROM accounts a
JOIN customers c ON c.customer_id = a.customer_id
JOIN branches b ON b.branch_id = c.branch_id
WHERE a.closed_date IS NULL
GROUP BY c.customer_id
ORDER BY total_balance DESC
LIMIT 10""",
     "answer": _top_depositors},
    {"id": "cohort",
     "question": "Of the customers who joined last year, how many are still with us and active?",
     "why": "Cohort view by month of joining: still a customer, and made a payment themselves in the last 30 days.",
     "sql": """SELECT substr(c.joined_date, 1, 7) AS joined_month,
       COUNT(*) AS joined,
       SUM(c.closed_date IS NULL) AS still_customers,
       SUM(c.closed_date IS NULL AND r.customer_id IS NOT NULL) AS active_last_30_days
FROM customers c
LEFT JOIN (SELECT DISTINCT customer_id FROM transactions
           WHERE ts >= '{d30}' AND channel <> 'Automatic') r ON r.customer_id = c.customer_id
WHERE c.joined_date >= '{last_year}-01-01' AND c.joined_date < '{this_year}-01-01'
GROUP BY joined_month
ORDER BY joined_month""",
     "answer": _cohort},
    {"id": "fee_waiver",
     "question": "What would it cost to waive account fees for customers under 25?",
     "why": "A what-if on the last 12 months of fees.",
     "sql": """SELECT COUNT(DISTINCT CASE WHEN c.age_band = '18-24' THEN t.customer_id END) AS customers_18_24,
       ROUND(SUM(CASE WHEN c.age_band = '18-24' THEN t.amount ELSE 0 END), 2) AS fees_18_24,
       ROUND(SUM(t.amount), 2) AS all_fees,
       ROUND(100.0 * SUM(CASE WHEN c.age_band = '18-24' THEN t.amount ELSE 0 END) / SUM(t.amount), 2) AS share_pct
FROM transactions t
JOIN customers c ON c.customer_id = t.customer_id
WHERE t.type = 'Fee' AND t.ts >= '{d365}'""",
     "answer": _fee_waiver},
]

BY_QUESTION = {e["question"]: e for e in EXAMPLES}


def run(example, conn, now):
    from . import guard
    sql = example["sql"].format(**_dates(now))
    df, _ = guard.run(conn, sql)
    try:
        text = example["answer"](df, now)
    except Exception as e:  # pragma: no cover
        text = f"(Could not summarise the result: {e})"
    return Answer(text, sql=sql, data=df, source="saved", understood=[f"**Saved example.** {example['why']}",
                                                                       "SQL written and checked by hand; it runs live on today's data."])
