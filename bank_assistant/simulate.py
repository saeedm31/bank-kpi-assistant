"""A simulated Portuguese retail bank: branches, customers, accounts, payments and loans.

Everything is relative to "now" in Lisbon time: 24 full months of history plus
the current month up to the current minute. Each month is generated from its own
seed, so the past stays the same while new payments keep arriving during the day.
That gives "today" and "last 24 hours" questions live answers.

Patterns built in (so there is something to find):
- Card payments grow and ATM cash use falls over time; December peaks, summer travel.
- More customers move to the app every month; older customers use branches more.
- Portugal pays 14 salaries: an extra one in June and in November.
- Customers who leave often stop receiving their salary one to two months before.
- Late payments on personal loans rise in the Algarve over the last months.
- Interest rates follow the 2022-2025 cycle (up, then down).
- One unusual event yesterday and one today, for the alerts page.
"""

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Lisbon")
except Exception:  # pragma: no cover - no tz database
    TZ = None


@dataclass(frozen=True)
class Settings:
    customers: int = 2000
    months: int = 24
    seed: int = 11


BRANCHES = pd.DataFrame(
    [(1, "Porto Baixa", "Porto", "Norte"), (2, "Porto Boavista", "Porto", "Norte"),
     (3, "Braga", "Braga", "Norte"), (4, "Aveiro", "Aveiro", "Centro"),
     (5, "Coimbra", "Coimbra", "Centro"), (6, "Lisboa Chiado", "Lisboa", "Lisboa"),
     (7, "Lisboa Parque das Nações", "Lisboa", "Lisboa"), (8, "Faro", "Faro", "Algarve")],
    columns=["branch_id", "name", "city", "region"])
BRANCH_P = np.array([.14, .12, .10, .08, .10, .18, .16, .12])

SEGMENTS = np.array(["Retail", "Premium", "Business"])
SEGMENT_P = [.78, .15, .07]
AGE_BANDS = np.array(["18-24", "25-34", "35-44", "45-54", "55-64", "65+"])
AGE_P = {"Retail": [.10, .20, .21, .19, .15, .15], "Premium": [.02, .12, .24, .26, .20, .16],
         "Business": [.0, .15, .30, .30, .18, .07]}
APP_AT_START = np.array([.85, .78, .66, .50, .34, .14])     # share using the app at the start, by age band
APP_ADOPT_YEARS = np.array([1.5, 2, 3, 4, 6, 12])          # mean years until a non-user adopts

CATEGORIES = np.array(["Groceries", "Restaurants", "Online shopping", "Fuel", "Clothing",
                       "Entertainment", "Home & DIY", "Health", "Travel"])
CATEGORY_P = np.array([.30, .17, .14, .08, .08, .07, .06, .06, .04])
CATEGORY_EUR = np.array([32, 21, 42, 48, 52, 19, 58, 28, 170.0])   # median amount
CARD_SEASON = np.array([.95, .9, .97, 1, 1, 1.03, 1.08, 1.1, .98, 1, 1.05, 1.3])

# Hour-of-day profiles (relative weights for hours 0..23)
H_CARD = np.array([1, .5, .3, .2, .2, .4, 1, 3, 5, 6, 7, 9, 12, 11, 8, 7, 8, 10, 12, 11, 8, 5, 3, 2.])
H_ATM = np.array([1, .5, .3, .2, .2, .3, .8, 2, 4, 5, 6, 7, 8, 6, 5, 5, 6, 8, 9, 7, 5, 3, 2, 1.])
H_APP = np.array([2, 1, .5, .3, .3, .5, 1, 3, 4, 4, 4, 5, 6, 5, 4, 4, 5, 6, 7, 8, 9, 9, 7, 4.])
H_WEB = np.array([.5, .2, .1, .1, .1, .2, .5, 1, 3, 6, 7, 6, 4, 5, 7, 7, 6, 4, 3, 3, 2, 2, 1, 1.])
H_BRANCH = np.array([0, 0, 0, 0, 0, 0, 0, 0, 0, 6, 9, 9, 6, 5, 8, 7, 0, 0, 0, 0, 0, 0, 0, 0.])
H_AUTO = np.array([0, 2, 4, 5, 3, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.])

TXN_COLUMNS = ["ts", "customer_id", "account_id", "amount", "direction", "type", "channel", "merchant_category"]


def lisbon_now():
    now = dt.datetime.now(TZ) if TZ else dt.datetime.now()
    return now.replace(tzinfo=None, microsecond=0)


def policy_rate(day):
    """A rough euro interest-rate path: low, up in 2022-23, down from mid-2024."""
    points = [(dt.date(2015, 1, 1), 0.0), (dt.date(2022, 6, 1), 0.0), (dt.date(2023, 10, 1), 4.0),
              (dt.date(2024, 6, 1), 3.9), (dt.date(2025, 6, 1), 2.1), (dt.date(2030, 1, 1), 2.0)]
    x = np.array([p[0].toordinal() for p in points])
    y = np.array([p[1] for p in points])
    return np.interp(np.asarray(day, dtype=float), x, y)


def _month_starts(now, months):
    first = dt.date(now.year, now.month, 1)
    out = []
    for k in range(months, -1, -1):
        y, m = divmod(first.year * 12 + first.month - 1 - k, 12)
        out.append(dt.date(y, m + 1, 1))
    return out


def _month_end(d):
    nxt = dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return nxt - dt.timedelta(days=1)


def _ordinals(dates):
    return np.array([d.toordinal() for d in dates], dtype=np.int64)


# ---------------------------------------------------------------- customers, accounts, loans

def _customers(rng, s, start, today):
    n = s.customers
    seg = rng.choice(SEGMENTS, n, p=SEGMENT_P)
    age = np.array([rng.choice(AGE_BANDS, p=AGE_P[g]) for g in seg])
    age_i = np.searchsorted(AGE_BANDS, age)
    branch = rng.choice(BRANCHES["branch_id"].to_numpy(), n, p=BRANCH_P)
    median = np.select([seg == "Retail", seg == "Premium"], [1450, 5200], 24000)
    income = np.round(median * rng.lognormal(0, np.where(seg == "Business", .6, .32)), 2)

    s0, t0 = start.toordinal(), today.toordinal()
    inside = rng.random(n) < .18
    joined = np.where(inside, s0 + (rng.random(n) ** .8) * (t0 - s0 - 1),
                      s0 - rng.random(n) * 12 * 365).astype(np.int64)
    closes = (rng.random(n) < .07) & (joined < t0 - 120)
    closed_lo = np.maximum(joined + 90, s0 + 30)
    closed = np.where(closes, closed_lo + rng.random(n) * np.maximum(t0 - 1 - closed_lo, 1), 0).astype(np.int64)
    closed = np.where(closes & (closed < t0), closed, 0)

    on_app = rng.random(n) < APP_AT_START[age_i]
    adopt = s0 + rng.exponential(APP_ADOPT_YEARS[age_i] * 365)
    app_since = np.where(on_app, s0 - rng.random(n) * 4 * 365, adopt)
    app_since = np.maximum(app_since, joined)
    app_since = np.where((app_since < t0) & ((closed == 0) | (app_since < closed)), app_since, 0).astype(np.int64)

    employed = np.where(age == "65+", rng.random(n) < .95, rng.random(n) < .9) & (seg != "Business")
    # most leavers have their salary moved elsewhere 1-2 months before closing,
    # and a few current customers are doing that right now (they will leave soon)
    salary_stop = np.where((closed > 0) & (rng.random(n) < .75), closed - rng.integers(30, 75, n), 0)
    soon = (closed == 0) & (joined < t0 - 240) & (rng.random(n) < .025)
    salary_stop = np.where(soon, t0 - rng.integers(48, 85, n), salary_stop)

    return pd.DataFrame({
        "customer_id": np.arange(10001, 10001 + n), "segment": seg, "age_band": age, "branch_id": branch,
        "joined": joined, "closed": closed, "app_since": app_since, "income": income,
        "employed": employed, "salary_stop": salary_stop, "age_i": age_i})


def _accounts(rng, cust, today):
    t0 = today.toordinal()
    rows = []
    seg = cust["segment"].to_numpy()
    for product, p in (("Current account", {"Retail": 1, "Premium": 1, "Business": 1}),
                       ("Savings account", {"Retail": .45, "Premium": .85, "Business": .3}),
                       ("Term deposit", {"Retail": .10, "Premium": .40, "Business": .15})):
        prob = np.array([p[g] for g in seg])
        has = rng.random(len(cust)) < prob
        opened = cust["joined"].to_numpy() + (0 if product == "Current account"
                                               else (rng.random(len(cust)) * 4 * 365).astype(np.int64))
        closed = cust["closed"].to_numpy()
        keep = has & (opened < t0) & ((closed == 0) | (opened < closed - 30))
        rows.append(pd.DataFrame({"customer_id": cust["customer_id"].to_numpy()[keep], "product": product,
                                  "opened": opened[keep], "closed": cust["closed"].to_numpy()[keep]}))
    acc = pd.concat(rows, ignore_index=True)
    acc = acc.sort_values(["customer_id", "opened"], kind="mergesort").reset_index(drop=True)
    acc.insert(0, "account_id", np.arange(50001, 50001 + len(acc)))
    return acc


def _loans(rng, cust, start, today):
    s0, t0 = start.toordinal(), today.toordinal()
    seg = cust["segment"].to_numpy()
    p_loan = np.select([seg == "Retail", seg == "Premium"], [.42, .55], .6)
    n_loans = (rng.random(len(cust)) < p_loan).astype(int) + (rng.random(len(cust)) < p_loan * .25)
    idx = np.repeat(np.arange(len(cust)), n_loans)
    c = cust.iloc[idx].reset_index(drop=True)
    n = len(c)
    seg = c["segment"].to_numpy()
    retail_products = np.array(["Mortgage", "Personal loan", "Car loan"])
    product = np.where(seg == "Business", "Business loan",
                       np.where(seg == "Premium", rng.choice(retail_products, n, p=[.6, .2, .2]),
                                rng.choice(retail_products, n, p=[.38, .42, .20])))
    issued = t0 - rng.random(n) * 10 * 365      # steady lending over ten years
    issued = np.maximum(issued, c["joined"].to_numpy() + 30).astype(np.int64)
    closed = c["closed"].to_numpy()
    keep = (issued < t0) & ((closed == 0) | (issued < closed - 60))
    c, product, issued, seg = c[keep].reset_index(drop=True), product[keep], issued[keep], seg[keep]
    n = len(c)

    term = np.select([product == "Mortgage", product == "Personal loan", product == "Car loan"],
                     [rng.choice([240, 300, 360, 420], n), rng.choice([36, 60, 84], n),
                      rng.choice([48, 60, 72, 84], n)], rng.choice([36, 60, 84, 120], n))
    size = np.select([product == "Mortgage", product == "Personal loan", product == "Car loan"],
                     [160000, 9000, 19000], 70000) * rng.lognormal(0, .45, n)
    size = np.round(size * np.where(seg == "Premium", 1.6, 1.0), -2)
    spread = np.select([product == "Mortgage", product == "Personal loan", product == "Car loan"],
                       [1.0, 6.0, 4.0], 3.0)
    rate = np.round(np.maximum(policy_rate(issued) + spread + rng.normal(0, .3, n), .5), 2)
    branch_region = c["branch_id"].map(BRANCHES.set_index("branch_id")["region"]).to_numpy()
    return pd.DataFrame({"loan_id": np.arange(70001, 70001 + n), "customer_id": c["customer_id"].to_numpy(),
                         "product": product, "issued": issued, "principal": size, "interest_rate": rate,
                         "term_months": term, "segment": seg, "region": branch_region})


def _outstanding(principal, rate, term, months_paid):
    r = np.maximum(rate, .01) / 1200
    k = np.clip(months_paid, 0, term)
    paid_share = ((1 + r) ** k - 1) / ((1 + r) ** term - 1)
    return np.round(principal * (1 - paid_share), 2)


def _instalment(principal, rate, term):
    r = np.maximum(rate, .01) / 1200
    return np.round(principal * r / (1 - (1 + r) ** -term), 2)


def _delinquency(rng, loans, snaps, today):
    """Days past due for every loan at every snapshot date (monthly Markov chain)."""
    n, m = len(loans), len(snaps)
    product = loans["product"].to_numpy()
    base = np.select([product == "Mortgage", product == "Personal loan", product == "Car loan"],
                     [.004, .012, .008], .009)
    base = base * np.where(loans["segment"].to_numpy() == "Premium", .5, 1)
    personal = product == "Personal loan"
    rise = np.where(personal, np.where(loans["region"].to_numpy() == "Algarve", .6, .15), 0)
    state = np.where(rng.random(n) < .965, 0, rng.integers(1, 5, n))  # 0 ok, 1..3 = 30/60/90 late, 4 = 90+
    out = np.zeros((n, m), dtype=np.int64)
    cure = np.array([0, .5, .3, .15, .01])
    months_to_today = np.array([(today.year - d.year) * 12 + today.month - d.month for d in snaps])
    issued = loans["issued"].to_numpy()
    for j in range(m):
        state = np.where(issued > snaps[j].toordinal() - 30, 0, state)
        boost = 1 + rise * max(0, 8 - months_to_today[j])
        u = rng.random(n)
        new_state = state.copy()
        ok = state == 0
        new_state[ok & (u < base * boost)] = 1
        late = ~ok
        cured = late & (u < cure[state])
        new_state[cured] = 0
        new_state[late & ~cured] = np.minimum(state[late & ~cured] + 1, 4)
        state = new_state
        jitter = rng.integers(0, 29, n)
        out[:, j] = np.where(state == 0, 0, (state - 1) * 30 + 1 + jitter)
        out[state == 4, j] = 91 + jitter[state == 4] + 30 * rng.integers(0, 4, (state == 4).sum())
    return out


# ---------------------------------------------------------------- payments

def _sample_hours(rng, n, weights):
    return rng.choice(24, n, p=weights / weights.sum())


def _events(rng, grid, counts, hours, month_start, weekdays_only=False):
    """Repeat grid rows by count and give each event a random time inside the customer's active days."""
    idx = np.repeat(np.arange(len(grid)), counts)
    g = grid.iloc[idx]
    lo, hi = g["lo"].to_numpy(), g["hi"].to_numpy()
    day = lo + np.floor(rng.random(len(g)) * (hi - lo + 1)).astype(np.int64)
    if weekdays_only:
        wd = (day + month_start.toordinal() - 1) % 7  # ordinal 1 = Monday
        day = np.where(wd == 6, day - 1, np.where(wd == 0, day + 1, day))   # Sat -> Fri, Sun -> Mon
        day = np.clip(day, lo, hi)
    hour = _sample_hours(rng, len(g), hours)
    secs = hour * 3600 + rng.integers(0, 3600, len(g))
    base = np.datetime64(month_start, "s")
    ts = base + ((day - 1) * 86400 + secs).astype("timedelta64[s]")
    return g.reset_index(drop=True), ts


def _frame(g, ts, amount, direction, kind, channel, category=None, account_col="current"):
    n = len(g)
    return pd.DataFrame({"ts": ts, "customer_id": g["customer_id"].to_numpy(),
                         "account_id": g[account_col].to_numpy(), "amount": np.round(amount, 2),
                         "direction": direction, "type": kind,
                         "channel": channel if not np.isscalar(channel) else np.full(n, channel),
                         "merchant_category": category if category is not None else np.full(n, None, dtype=object)})


def _month_payments(s, cust, acc, loans, dpd_by_month, month_start, window_start, today):
    rng = np.random.default_rng([s.seed, month_start.year, month_start.month])
    m_end = _month_end(month_start)
    ms, me = month_start.toordinal(), m_end.toordinal()
    years = (ms - window_start.toordinal()) / 365.0
    month = month_start.month

    active = (cust["joined"] <= me) & ((cust["closed"] == 0) | (cust["closed"] > ms))
    grid = cust[active].copy()
    grid["lo"] = np.maximum(grid["joined"] - ms + 1, 1)
    grid["hi"] = np.where(grid["closed"] > 0, np.minimum(grid["closed"] - ms, me - ms + 1), me - ms + 1)
    grid = grid[grid["hi"] >= grid["lo"]]
    current = acc[acc["product"] == "Current account"].set_index("customer_id")["account_id"]
    grid["current"] = grid["customer_id"].map(current).to_numpy()
    n = len(grid)
    seg = grid["segment"].to_numpy()
    prem = seg == "Premium"
    biz = seg == "Business"
    old = grid["age_band"].to_numpy() == "65+"
    leaving = ((grid["closed"] > 0) & (grid["closed"] - me < 45)) | \
              ((grid["closed"] == 0) & (grid["salary_stop"] > 0) & (grid["salary_stop"] < me + 30))
    on_app = (grid["app_since"] > 0) & (grid["app_since"] <= me)
    parts = []

    # salaries and pensions (an extra one in June and November)
    paid = grid["employed"].to_numpy() & ((grid["salary_stop"] == 0) | (grid["salary_stop"] > ms + 25)).to_numpy()
    paid &= (grid["hi"] >= 24).to_numpy()
    g = grid[paid]
    day = np.clip(rng.integers(24, 29, len(g)), g["lo"].to_numpy(), g["hi"].to_numpy())
    hour = rng.integers(1, 6, len(g))
    ts = np.datetime64(month_start, "s") + ((day - 1) * 86400 + hour * 3600 + rng.integers(0, 3600, len(g))).astype("timedelta64[s]")
    kind = np.where(g["age_band"].to_numpy() == "65+", "Pension", "Salary")
    pay = g["income"].to_numpy() * np.where(kind == "Pension", .7, 1) * rng.normal(1, .02, len(g))
    pay = pay * (2 if month in (6, 11) else 1) * (1.03 ** years)
    parts.append(_frame(g, ts, pay, "in", kind, "Automatic"))

    # business income: many incoming transfers
    lam = np.where(biz, 10 * (1.15 if month in (6, 7, 12) else 1), 0) * ~leaving.to_numpy()
    g, ts = _events(rng, grid, rng.poisson(lam), H_WEB, month_start)
    amt = g["income"].to_numpy() / 10 * rng.lognormal(-.18, .6, len(g)) * (1.03 ** years)
    parts.append(_frame(g, ts, amt, "in", "Transfer in", "Automatic"))

    # card payments
    lam = np.select([prem, biz], [11, 6], 7) * CARD_SEASON[month - 1] * (1.08 ** years)
    lam = lam * np.where(leaving, .45, 1) * np.where(old, .7, 1)
    g, ts = _events(rng, grid, rng.poisson(lam), H_CARD, month_start)
    cat_p = CATEGORY_P.copy()
    if month in (7, 8):
        cat_p[CATEGORIES == "Travel"] *= 2.5
    if month in (12, 1):
        cat_p[CATEGORIES == "Clothing"] *= 2
    ci = rng.choice(len(CATEGORIES), len(g), p=cat_p / cat_p.sum())
    mult = np.select([g["segment"].to_numpy() == "Premium", g["segment"].to_numpy() == "Business"], [1.8, 1.5], 1)
    amt = CATEGORY_EUR[ci] * mult * rng.lognormal(0, .55, len(g))
    parts.append(_frame(g, ts, amt, "out", "Card payment", "Card", CATEGORIES[ci]))

    # ATM cash, falling over time
    lam = np.where(biz, .6, 1.7) * (.86 ** years) * np.where(old, 1.8, 1)
    g, ts = _events(rng, grid, rng.poisson(lam), H_ATM, month_start)
    amt = rng.choice([20, 40, 50, 60, 100, 150, 200], len(g), p=[.18, .2, .2, .14, .16, .06, .06])
    parts.append(_frame(g, ts, amt.astype(float), "out", "ATM withdrawal", "ATM"))

    # transfers out: app, web or branch
    lam = np.select([prem, biz], [3, 9], 1.9)
    g, _ = _events(rng, grid, rng.poisson(lam), H_WEB, month_start)
    app_user = ((g["app_since"] > 0) & (g["app_since"] <= ms + 15)).to_numpy()
    u = rng.random(len(g))
    channel = np.where(app_user, np.where(u < .78, "App", np.where(u < .98, "Web", "Branch")),
                       np.where(u < .3, "Web", "Branch"))
    hours = np.where(channel == "App", _sample_hours(rng, len(g), H_APP),
                     np.where(channel == "Web", _sample_hours(rng, len(g), H_WEB), _sample_hours(rng, len(g), H_BRANCH)))
    day = g["lo"].to_numpy() + np.floor(rng.random(len(g)) * (g["hi"] - g["lo"] + 1).to_numpy()).astype(np.int64)
    wd = (day + ms - 1) % 7
    weekend_branch = (channel == "Branch") & ((wd == 6) | (wd == 0))
    day = np.where(weekend_branch & (wd == 6), day - 1, np.where(weekend_branch, day + 1, day))
    day = np.clip(day, g["lo"].to_numpy(), g["hi"].to_numpy())
    ts = np.datetime64(month_start, "s") + ((day - 1) * 86400 + hours * 3600 + rng.integers(0, 3600, len(g))).astype("timedelta64[s]")
    med = np.select([g["segment"].to_numpy() == "Premium", g["segment"].to_numpy() == "Business"], [800, 1700], 190)
    parts.append(_frame(g, ts, np.minimum(med * rng.lognormal(0, .9, len(g)), 30000), "out", "Transfer out", channel))

    # cash deposits at the branch
    g, ts = _events(rng, grid, rng.poisson(np.where(biz, 2, .3)), H_BRANCH, month_start, weekdays_only=True)
    med = np.where(g["segment"].to_numpy() == "Business", 1500, 300)
    parts.append(_frame(g, ts, med * rng.lognormal(0, .6, len(g)), "in", "Cash deposit", "Branch"))

    # direct debits (utilities, phone, insurance)
    g, ts = _events(rng, grid, np.where(biz, 4, 2), H_AUTO, month_start)
    parts.append(_frame(g, ts, 80 * rng.lognormal(0, .5, len(g)), "out", "Direct debit", "Automatic"))

    # monthly account fee on the first active day
    fee = np.select([prem, biz], [0, 12.5], 4.5)
    g = grid[fee > 0]
    f = fee[fee > 0]
    ts = np.datetime64(month_start, "s") + ((g["lo"].to_numpy() - 1) * 86400 + 4 * 3600 + rng.integers(0, 3600, len(g))).astype("timedelta64[s]")
    parts.append(_frame(g, ts, f, "out", "Fee", "Automatic"))

    # loan instalments, unless the loan is late this month
    if len(loans):
        lo = loans[(loans["issued"] < ms) & (loans["end"] > ms)]
        late = dpd_by_month.get(month_start, pd.Series(dtype=int)).reindex(lo["loan_id"]).fillna(0).to_numpy() > 0
        lo = lo[~late]
        lg = lo.merge(grid[["customer_id", "lo", "hi", "current"]], on="customer_id")
        day = np.clip(rng.integers(1, 6, len(lg)), lg["lo"].to_numpy(), lg["hi"].to_numpy())
        ts = np.datetime64(month_start, "s") + ((day - 1) * 86400 + 3 * 3600 + rng.integers(0, 3600, len(lg))).astype("timedelta64[s]")
        parts.append(_frame(lg, ts, lg["instalment"].to_numpy(), "out", "Loan repayment", "Automatic"))

    out = pd.concat(parts, ignore_index=True)
    return out[out["ts"] < np.datetime64(today + dt.timedelta(days=1), "s")]


# ---------------------------------------------------------------- unusual events

EVENTS = ("card_outage", "big_transfers", "atm_rush")


def _inject(tx, cust, day, kind, rng):
    """Add one unusual event on `day` (yesterday or today) so the alerts page has something real to find."""
    branch_of = cust.set_index("customer_id")["branch_id"]
    b = int(rng.integers(1, 9))
    name = BRANCHES.set_index("branch_id").loc[b, "name"]
    start = np.datetime64(day, "s") + np.timedelta64(int(rng.integers(7, 9)) * 3600, "s")
    if kind == "card_outage":
        stop = start + np.timedelta64(10 * 3600, "s")
        hit = ((tx["type"] == "Card payment") & (tx["ts"] >= start) & (tx["ts"] < stop)
               & (tx["customer_id"].map(branch_of) == b) & (pd.Series(rng.random(len(tx))) < .85).to_numpy())
        tx = tx[~hit]
        what = f"Card payments by {name} customers fell sharply from {str(start)[11:16]} (simulated card terminal outage)"
    elif kind == "big_transfers":
        pool = cust[(cust["branch_id"] == b) & (cust["segment"] == "Business") & (cust["closed"] == 0)]
        if pool.empty:
            pool = cust[(cust["branch_id"] == b) & (cust["closed"] == 0)]
        k = 9
        who = pool.sample(k, replace=True, random_state=int(rng.integers(1 << 30)))
        ts = start + (rng.random(k) * 3 * 3600).astype("timedelta64[s]")
        extra = pd.DataFrame({"ts": ts, "customer_id": who["customer_id"].to_numpy(),
                              "account_id": who["current"].to_numpy(),
                              "amount": np.round(rng.uniform(18000, 60000, k), 2), "direction": "out",
                              "type": "Transfer out", "channel": "Web", "merchant_category": None})
        tx = pd.concat([tx, extra], ignore_index=True)
        what = f"Nine large transfers out (€18k-60k) by {name} business customers within three hours"
    else:  # atm_rush
        pool = cust[(cust["branch_id"] == b) & (cust["closed"] == 0)]
        k = 160
        who = pool.sample(k, replace=True, random_state=int(rng.integers(1 << 30)))
        ts = start + (rng.random(k) * 9 * 3600).astype("timedelta64[s]")
        extra = pd.DataFrame({"ts": ts, "customer_id": who["customer_id"].to_numpy(),
                              "account_id": who["current"].to_numpy(),
                              "amount": rng.choice([100., 150., 200.], k), "direction": "out",
                              "type": "ATM withdrawal", "channel": "ATM", "merchant_category": None})
        tx = pd.concat([tx, extra], ignore_index=True)
        what = f"ATM cash withdrawals by {name} customers well above normal (simulated local festival)"
    return tx, {"date": str(day), "kind": kind, "branch": name, "starts": str(start)[:16], "what": what}


# ---------------------------------------------------------------- main entry

def simulate(now=None, settings=Settings()):
    """Return a dict of DataFrames: the bank's tables up to `now`, plus later-today rows in 'pending'."""
    s = settings
    now = now or lisbon_now()
    today = now.date()
    months = _month_starts(now, s.months)
    window_start = months[0]
    rng = np.random.default_rng([s.seed, window_start.year, window_start.month])

    cust = _customers(rng, s, window_start, today)
    acc = _accounts(rng, cust, today)
    cust["current"] = cust["customer_id"].map(acc[acc["product"] == "Current account"].set_index("customer_id")["account_id"])
    loans = _loans(rng, cust, window_start, today)
    loans["instalment"] = _instalment(loans["principal"].to_numpy(), loans["interest_rate"].to_numpy(),
                                      loans["term_months"].to_numpy())
    issued_dates = pd.to_datetime(loans["issued"].map(dt.date.fromordinal))
    end = issued_dates + pd.to_timedelta(loans["term_months"].to_numpy() * 30.44, unit="D")
    loans["end"] = _ordinals(end.dt.date)
    # a customer who closes repays early
    closed_of = loans["customer_id"].map(cust.set_index("customer_id")["closed"]).to_numpy()
    loans["end"] = np.where((closed_of > 0) & (closed_of < loans["end"]), closed_of, loans["end"])

    snap_dates = [_month_end(m) for m in months[:-1]] + [today]
    dpd = _delinquency(rng, loans, snap_dates, today)
    dpd_by_month = {m: pd.Series(dpd[:, j], index=loans["loan_id"]) for j, m in enumerate(months)}

    tx = pd.concat([_month_payments(s, cust, acc, loans, dpd_by_month, m, window_start, today) for m in months],
                   ignore_index=True)

    events = []
    for k, day in enumerate((today - dt.timedelta(days=1), today)):
        erng = np.random.default_rng([s.seed, day.toordinal(), 99])
        kind = EVENTS[(day.toordinal() + k) % len(EVENTS)]
        tx, info = _inject(tx, cust, day, kind, erng)
        events.append(info)

    tx = tx.sort_values("ts", kind="mergesort").reset_index(drop=True)
    cutoff = np.datetime64(now, "s")
    pending = tx[tx["ts"] > cutoff].reset_index(drop=True)
    tx = tx[tx["ts"] <= cutoff].reset_index(drop=True)

    balances = _balances(cust, acc, snap_dates, s)
    loan_snaps = _loan_snapshots(loans, dpd, snap_dates)
    return _finish(cust, acc, loans, tx, pending, balances, loan_snaps, dpd, snap_dates, events, now)


def _balances(cust, acc, snap_dates, s):
    """End-of-month balance of every open account (the current month uses today)."""
    rng = np.random.default_rng([s.seed, 7])
    a = acc.merge(cust[["customer_id", "segment", "income"]], on="customer_id")
    n = len(a)
    habit = rng.uniform(.25, 1.4, n)
    pot = a["income"].to_numpy() * rng.lognormal(np.log(9), .8, n)
    saving = a["income"].to_numpy() * rng.uniform(0, .12, n)
    term = np.round(np.select([a["segment"].to_numpy() == "Premium", a["segment"].to_numpy() == "Business"],
                              [80000, 50000], 22000) * rng.lognormal(0, .5, n), -2)
    rows = []
    for j, d in enumerate(snap_dates):
        o = d.toordinal()
        open_ = (a["opened"].to_numpy() <= o) & ((a["closed"].to_numpy() == 0) | (a["closed"].to_numpy() > o))
        months_open = np.maximum((o - a["opened"].to_numpy()) / 30.44, 0)
        cur = a["income"].to_numpy() * habit * rng.normal(1, .15, n) * np.where(a["segment"] == "Business", 1.5, 1)
        sav = pot + saving * np.minimum(months_open, 60) * rng.normal(1, .05, n)
        bal = np.select([a["product"] == "Current account", a["product"] == "Savings account"], [cur, sav], term)
        rows.append(pd.DataFrame({"snapshot_date": d.isoformat(), "account_id": a["account_id"].to_numpy()[open_],
                                  "balance": np.round(bal[open_], 2)}))
    return pd.concat(rows, ignore_index=True)


def _loan_snapshots(loans, dpd, snap_dates):
    rows = []
    for j, d in enumerate(snap_dates):
        o = d.toordinal()
        live = (loans["issued"].to_numpy() <= o) & (loans["end"].to_numpy() > o)
        months_paid = (o - loans["issued"].to_numpy()) / 30.44
        out = _outstanding(loans["principal"].to_numpy(), loans["interest_rate"].to_numpy(),
                           loans["term_months"].to_numpy(), months_paid)
        rows.append(pd.DataFrame({"snapshot_date": d.isoformat(), "loan_id": loans["loan_id"].to_numpy()[live],
                                  "outstanding": out[live], "days_past_due": dpd[live, j]}))
    return pd.concat(rows, ignore_index=True)


def _iso(ordinals):
    return [dt.date.fromordinal(int(o)).isoformat() if o > 0 else None for o in ordinals]


def _finish(cust, acc, loans, tx, pending, balances, loan_snaps, dpd, snap_dates, events, now):
    customers = pd.DataFrame({
        "customer_id": cust["customer_id"], "segment": cust["segment"], "age_band": cust["age_band"],
        "branch_id": cust["branch_id"], "joined_date": _iso(cust["joined"]), "closed_date": _iso(cust["closed"]),
        "app_user_since": _iso(cust["app_since"])})
    latest = balances[balances["snapshot_date"] == snap_dates[-1].isoformat()].set_index("account_id")["balance"]
    accounts = pd.DataFrame({
        "account_id": acc["account_id"], "customer_id": acc["customer_id"], "product": acc["product"],
        "opened_date": _iso(acc["opened"]), "closed_date": _iso(acc["closed"]),
        "balance": acc["account_id"].map(latest).fillna(0).round(2)})
    last = snap_dates[-1].toordinal()
    live = (loans["end"].to_numpy() > last)
    months_paid = (last - loans["issued"].to_numpy()) / 30.44
    out = np.where(live, _outstanding(loans["principal"].to_numpy(), loans["interest_rate"].to_numpy(),
                                      loans["term_months"].to_numpy(), months_paid), 0)
    now_dpd = np.where(live, dpd[:, -1], 0)
    status = np.where(~live, "Repaid", np.where(now_dpd > 90, "Default", np.where(now_dpd > 0, "Late", "Current")))
    loans_out = pd.DataFrame({
        "loan_id": loans["loan_id"], "customer_id": loans["customer_id"], "product": loans["product"],
        "issued_date": _iso(loans["issued"]), "principal": loans["principal"], "interest_rate": loans["interest_rate"],
        "term_months": loans["term_months"], "monthly_payment": loans["instalment"], "outstanding": out,
        "days_past_due": now_dpd, "status": status})

    def txn(df):
        df = df.copy()
        df["ts"] = pd.to_datetime(df["ts"]).dt.strftime("%Y-%m-%d %H:%M:%S")
        return df[TXN_COLUMNS]

    return {"branches": BRANCHES.copy(), "customers": customers, "accounts": accounts, "loans": loans_out,
            "transactions": txn(tx), "pending": txn(pending), "balance_snapshots": balances,
            "loan_snapshots": loan_snaps, "events": events, "now": now}
