"""The built-in engine: understands questions about the bank's KPIs and writes the SQL itself.

No AI model and no API key. It reads the question for five things, using the
agreed KPI definitions in kpis.yml:
    metric      "card spend", "NPL ratio", "new customers" ...
    period      "last month", "since March", "today" ...
    breakdown   "by branch", "per month", "which region" ...
    filters     "in Porto", "premium customers", "mortgages" ...
    comparison  "vs last month", "year on year", "how did it change" ...
Anything it cannot place is reported, and questions outside its scope get a
clear "I can't answer that" instead of a guess.
"""

import datetime as dt
import difflib
import os
import re
from dataclasses import dataclass, field, replace

import pandas as pd
import yaml

from . import guard
from .timeparse import Period, add_months, find_period, previous, this_unit

HERE = os.path.dirname(os.path.abspath(__file__))

STOPWORDS = set("""
a an the of in on at for to from with and or by per is are was were be been being am do does did done have has had
what what's whats which who how much many show me tell give list please can could would you your i we us our my
there this that these those it its it's total overall value values amount amounts level levels figure figures number
numbers so far get got see let look looking like made make bank bank's banks's all any some about across into
current currently right now today's our all-time overall whole entire period time times during over between up down
customers customer clients client people accounts account currently far yet just also only else each every than
sum sums did doing much more less most least top highest lowest biggest largest smallest best worst fewest compare
compared comparison versus vs against change changed changes growth grew grow grown increase increased decrease
decreased rise rose fall fell drop dropped trend trends evolution over-time going went been doing performing perform
split broken break down breakdown happened happen happening look looks looking s 's please thanks thank kpi kpis
metric metrics report numbers stats statistics give shown display table chart graph plot see view
new joined join opened open issued granted lent spent spend received paid sent withdrawn taken out charged made
held hold holding have own owned we're there's let's give show
""".split())
DECLINE = set("""
who whose why predict prediction forecast forecasts will would likely probability correlation correlate correlated
relationship cause causes caused reason reasons both without never also except recommend should no not
send email e-mail delete remove cancel block unblock approve reject call contact notify close raise lower set
""".split())
ACTIONS = set("""increase decrease raise lower set change update send email delete remove cancel block unblock
approve reject call contact notify close open create add insert drop move pay transfer refund""".split())
SHARE_RE = re.compile(r"\b(percentage|percent|proportion|share|fraction)\b|%")
COUNT_CUES = re.compile(r"\b(how many|number of|count of|count)\b")
COMPARE_RE = re.compile(r"\b(?:compared (?:to|with)|vs\.?|versus|against|than|from|since) (?:the )?"
                        r"(?:same (?:period|time|days) )?(?:of )?(last|previous|prior) (day|week|month|quarter|year)\b")
YOY_RE = re.compile(r"\b(year on year|year over year|yoy|y/y|same period last year|on last year)\b")
MOM_RE = re.compile(r"\b(month on month|month over month|mom|week on week|day on day)\b")
CHANGE_RE = re.compile(r"\b(change|changed|changes|growth|grew|grow|grown|increase|increased|decrease|decreased|rise|"
                       r"rose|fall|fell|drop|dropped|up or down|higher or lower|compare|comparison|difference|"
                       r"better or worse)\b")
GRAIN_RE = re.compile(r"\b(?:by|per|each|every|for each|for every|split by) (hour|day|week|month|quarter|year)\b|"
                      r"\b(hourly|daily|weekly|monthly|quarterly|yearly|annually|annual)\b")
TREND_RE = re.compile(r"\b(over time|trend|trends|evolution|history|time series|over the months|month by month|"
                      r"how has .* (?:changed|evolved|developed)|how did .* (?:change|evolve|develop))\b")
GROUP_CUE = r"(?:by|per|for each|for every|across|split by|broken down by|between|among|each|every|which|what|top \d+|top)"
TOP_RE = re.compile(r"\btop (\d+)\b|\b(highest|most|biggest|largest|best|greatest|leading)\b")
BOTTOM_RE = re.compile(r"\bbottom (\d+)\b|\b(lowest|least|smallest|fewest|weakest)\b")
GRAIN_WORDS = {"hourly": "hour", "daily": "day", "weekly": "week", "monthly": "month", "quarterly": "quarter",
               "yearly": "year", "annually": "year", "annual": "year"}


@dataclass
class Query:
    metric: str
    period: Period
    group: list = field(default_factory=list)      # dimension ids or "grain:month" etc.
    filters: dict = field(default_factory=dict)    # dim -> [values]
    compare: bool = False
    yoy: bool = False
    order: str = "desc"
    limit: int = None
    ignored: list = field(default_factory=list)
    notes: list = field(default_factory=list)


@dataclass
class Answer:
    text: str
    sql: str = ""
    data: pd.DataFrame = None
    chart: dict = None            # {"kind": "bar"/"line", "x":..., "y":..., "color":...}
    understood: list = None       # bullet points: how the question was read
    ok: bool = True
    source: str = "built-in"


def normalise(text):
    t = text.lower().replace("’", "'").replace("€", " eur ")
    t = re.sub(r"(\d),(\d{3})", r"\1\2", t)
    t = re.sub(r"[^a-z0-9%+'\-/ ]+", " ", t)
    t = re.sub(r"\s+", " ", t)
    return f" {t.strip()} "


def _same_words(toks, phrase):
    """Token-by-token match that accepts simple plurals ("atms" = "atm")."""
    if len(toks) != len(phrase):
        return False
    return all(t == p or t == p + "s" or t == p + "es" or (p.endswith("s") and len(p) > 3 and t == p[:-1])
               for t, p in zip(toks, phrase))


def tokens(text):
    return re.findall(r"[a-z0-9%+]+(?:[-'/][a-z0-9+]+)*", text)


class Catalog:
    def __init__(self, path=os.path.join(HERE, "kpis.yml")):
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        self.sources = raw["sources"]
        self.dims = raw["dimensions"]
        self.metrics = raw["metrics"]
        self.value_aliases = raw.get("value_aliases", {})

    def dim_ok(self, dim, metric):
        d = self.dims[dim]
        return "sources" not in d or self.metrics[metric]["source"] in d["sources"]


class Engine:
    def __init__(self, conn, catalog=None):
        self.cat = catalog or Catalog()
        self.values = self._load_values(conn)
        self._metric_phrases = sorted(((tuple(tokens(normalise(w))), m) for m, d in self.cat.metrics.items()
                                       for w in [d["label"]] + d["words"]), key=lambda x: -len(x[0]))
        self._value_phrases = self._build_value_phrases()
        self._dim_phrases = sorted(((tuple(tokens(normalise(w))), dim) for dim, d in self.cat.dims.items()
                                    for w in d["words"]), key=lambda x: -len(x[0]))

    # ------------------------------------------------------------ setup
    def _load_values(self, conn):
        exprs = {"branch": "SELECT DISTINCT name FROM branches", "city": "SELECT DISTINCT city FROM branches",
                 "region": "SELECT DISTINCT region FROM branches", "segment": "SELECT DISTINCT segment FROM customers",
                 "age_band": "SELECT DISTINCT age_band FROM customers",
                 "channel": "SELECT DISTINCT channel FROM transactions",
                 "type": "SELECT DISTINCT type FROM transactions",
                 "category": "SELECT DISTINCT merchant_category FROM transactions",
                 "account_product": "SELECT DISTINCT product FROM accounts",
                 "loan_product": "SELECT DISTINCT product FROM loans"}
        return {dim: sorted(v for v in guard.run(conn, sql)[0].iloc[:, 0].tolist() if isinstance(v, str) and v)
                for dim, sql in exprs.items()}

    def _build_value_phrases(self):
        phrases = []
        for dim, vals in self.values.items():
            for v in vals:
                words = {v.lower(), v.lower() + "s"} | {a.lower() for a in self.cat.value_aliases.get(v, []) or []}
                if dim == "type" and v in ("Card payment", "ATM withdrawal", "Transfer out", "Transfer in"):
                    words = {a.lower() for a in self.cat.value_aliases.get(v, []) or []}
                for w in words:
                    t = tuple(tokens(normalise(w)))
                    if t:
                        phrases.append((t, dim, v))
        # a city and its only branch share a name (Faro, Braga...): read it as the city
        prefer = {"city": 0, "region": 1, "branch": 2}
        return sorted(phrases, key=lambda x: (-len(x[0]), prefer.get(x[1], 3)))

    # ------------------------------------------------------------ parsing
    def parse(self, question, now, previous_query=None):
        text = normalise(question)
        notes = []

        compare = yoy = False
        m = COMPARE_RE.search(text)
        if m:
            compare, yoy = True, m.group(2) == "year"
            text = text[:m.start()] + " " + text[m.end():]
        if YOY_RE.search(text):
            compare = yoy = True
            text = YOY_RE.sub(" ", text)
        if MOM_RE.search(text):
            compare = True
            text = MOM_RE.sub(" ", text)

        grain = None
        g = GRAIN_RE.search(text)
        if g:
            grain = g.group(1) or GRAIN_WORDS[g.group(2)]
            text = text[:g.start()] + " " + text[g.end():]
        trend = bool(TREND_RE.search(text)) and not compare   # "how did X change since last month" is a comparison

        period, text = find_period(text, now)
        if CHANGE_RE.search(text) and not grain and not trend:
            compare = True
        if trend and not grain:
            grain = "month"

        count_cue = bool(COUNT_CUES.search(text))
        top = TOP_RE.search(text)
        bottom = BOTTOM_RE.search(text)
        order = "asc" if bottom and not top else "desc"
        limit = None
        for mm in (top, bottom):
            if mm and mm.group(1):
                limit = int(mm.group(1))

        toks = tokens(text)
        used = [False] * len(toks)

        # breakdown: a group cue followed by a dimension word
        groups = []
        for i, tk in enumerate(toks):
            for phrase, dim in self._dim_phrases:
                n = len(phrase)
                if _same_words(toks[i:i + n], phrase) and not any(used[i:i + n]):
                    before = " ".join(toks[max(0, i - 3):i])
                    if re.search(rf"(?:^| ){GROUP_CUE}(?: the| our| each| every)?$", before) or \
                            (groups and before.endswith(" and") or before == "and") or \
                            (i == 0 and toks[i + n:i + n + 1] in (["has"], ["had"], ["with"], ["is"], ["was"])):
                        groups.append((dim, phrase))
                        for k in range(i, i + n):
                            used[k] = True
                        break

        found = self._match_all(toks, used)
        metric_hits = sorted([f for f in found if f[0] == "metric"], key=lambda f: (-f[2], f[3]))
        metrics_found = [f[1] for f in metric_hits]
        values = [f[1] for f in found if f[0] == "value"]
        if SHARE_RE.search(text) and metrics_found and \
                self.cat.metrics[metrics_found[0]]["unit"] != "pct":
            return None, self._decline(question, ["share / percentage of " + lc(self.cat.metrics[metrics_found[0]]["label"])])

        leftover = [t for t, u in zip(toks, used) if not u]
        ignored = [t for t in leftover if t not in STOPWORDS and not t.isdigit()
                   and t not in ("how", "many", "number", "count", "by", "per", "which", "top", "bottom")]

        # follow-up questions reuse the previous question's metric and settings
        follow_up = False
        if not metrics_found:
            if previous_query is None or ignored:
                return None, self._help(question, ignored)
            follow_up = True
            metric_id = previous_query.metric
        else:
            metric_id = metrics_found[0]
            if len(set(metrics_found)) > 1:
                notes.append(f"I answered about {self.cat.metrics[metric_id]['label'].lower()} only; "
                             f"ask about one number at a time.")
        if count_cue and self.cat.metrics[metric_id].get("count"):
            metric_id = self.cat.metrics[metric_id]["count"]

        first = tokens(normalise(question))[:1]
        if first and first[0] in ACTIONS:
            return None, self._decline(question, [f"'{first[0]}' asks me to change something; I only read data"])
        if any(w in DECLINE for w in leftover) or len(ignored) >= 3:
            return None, self._decline(question, ignored)

        src = self.cat.metrics[metric_id]["source"]
        dims = []
        for dim, phrase in groups:
            if dim in ("account_product", "loan_product"):
                dim = "loan_product" if src in ("loans", "loan_book") else "account_product"
            if self.cat.dim_ok(dim, metric_id):
                dims.append(dim)
            else:
                notes.append(f"{self.cat.metrics[metric_id]['label']} can't be split by "
                             f"{self.cat.dims[dim]['label'].lower()}, so I left that out.")
        filters = {}
        for dim, v in values:
            if dim in ("account_product", "loan_product"):
                if src in ("loans", "loan_book") and v in self.values["loan_product"]:
                    dim = "loan_product"
                elif src in ("balances", "new_accounts") and v in self.values["account_product"]:
                    dim = "account_product"
            met = self.cat.metrics[metric_id]
            definition = met["value"] + met.get("where", "")
            if self.cat.dims[dim]["expr"] in definition:
                if f"'{v}'" not in definition:
                    return None, self._decline(question, [f"{v} (conflicts with how {lc(met['label'])} is defined)"])
                notes.append(f"'{v}' is already part of how {lc(met['label'])} is defined, so I didn't filter on it.")
                continue
            if self.cat.dim_ok(dim, metric_id):
                filters.setdefault(dim, [])
                if v not in filters[dim]:
                    filters[dim].append(v)
            else:
                notes.append(f"'{v}' doesn't apply to {self.cat.metrics[metric_id]['label'].lower()}, so I ignored it.")

        group = ([f"grain:{grain}"] if grain else []) + dims
        if follow_up:
            pq = previous_query
            merged = dict(pq.filters)
            merged.update(filters)
            filters = merged
            group = group or ([] if (period or compare) and not pq.group else list(pq.group))
            period = period or pq.period
            compare = compare or (pq.compare and not group)
            yoy = yoy or pq.yoy
            notes.append(f"Follow-up: still about {self.cat.metrics[metric_id]['label'].lower()}.")
        if period is None:
            period = self.default_period(metric_id, grain, now)
        q = Query(metric_id, period, group[:2], filters, compare and not grain, yoy, order, limit, ignored, notes)
        if len(group) > 2:
            notes.append("I can split by two things at most.")
        return q, None

    def _match_all(self, toks, used):
        """Left to right, take the longest KPI or value phrase starting at each word (KPI wins a tie)."""
        candidates = [(p, "metric", m) for p, m in self._metric_phrases] + \
                     [(p, "value", (dim, v)) for p, dim, v in self._value_phrases]
        candidates.sort(key=lambda c: (-len(c[0]), c[1] != "metric"))   # stable: keeps the city-first order
        found = []
        i = 0
        while i < len(toks):
            hit = None
            if not used[i]:
                for phrase, kind, item in candidates:
                    n = len(phrase)
                    if _same_words(toks[i:i + n], phrase) and not any(used[i:i + n]):
                        hit = (kind, item, n, i)
                        break
            if hit:
                found.append(hit)
                for k in range(i, i + hit[2]):
                    used[k] = True
                i += hit[2]
            else:
                i += 1
        return found

    def default_period(self, metric_id, grain, now):
        snapshot = self.cat.sources[self.cat.metrics[metric_id]["source"]]["kind"] == "snapshot"
        today = dt.datetime(now.year, now.month, now.day)
        if grain == "hour":
            return replace(this_unit("day", now), explicit=False)
        if grain == "day":
            return Period(today - dt.timedelta(days=29), now, "the last 30 days", "span", True, True, False)
        if grain == "week":
            start = today - dt.timedelta(days=today.weekday() + 7 * 11)
            return Period(start, now, "the last 12 weeks", "span", True, True, False)
        if grain in ("month", "quarter", "year"):
            months = 24 if snapshot or grain != "month" else 12
            start = add_months(today.replace(day=1), -(months - 1))
            return Period(start, now, f"the last {months} months", "span", True, True, False)
        if snapshot:
            return Period(today, now, "now", "now", True, explicit=False)
        return replace(this_unit("month", now), explicit=False)

    def _help(self, question, ignored):
        vocab = [w for d in self.cat.metrics.values() for w in d["words"]]
        guess = []
        for word in ignored:
            for g in difflib.get_close_matches(word, vocab, n=2, cutoff=.75):
                if g not in guess:
                    guess.append(g)
        guess = guess[:3]
        lines = ["I couldn't find a KPI I know in that question."]
        if guess:
            lines.append("Did you mean: " + ", ".join(f"**{g}**" for g in guess) + "?")
        lines.append("I know about card spend, ATM cash, transfers, salaries, net inflow, fee income, active customers, "
                     "app users, new and lost customers, deposits, the loan book, NPL ratio, late loans, new lending "
                     "and interest rates. For anything else, switch on AI mode with your own API key.")
        return Answer("\n\n".join(lines), ok=False)

    def _decline(self, question, ignored):
        return Answer("This question needs a custom query that the built-in engine doesn't write "
                      f"(I couldn't place: *{', '.join(ignored)}*). Try one of the **saved examples**, "
                      "or switch on **AI mode** with your own API key to ask it in free form.", ok=False)

    # ------------------------------------------------------------ SQL
    def snapshot_date(self, period, now):
        """Snapshots exist at each month end and today. Pick the last one inside the period."""
        last = period.end - dt.timedelta(seconds=1)
        if last.date() >= now.date():
            return now.date()
        d = last.date()
        month_end = add_months(d.replace(day=1), 1) - dt.timedelta(days=1)
        return month_end if month_end == d else d.replace(day=1) - dt.timedelta(days=1)

    def _time_filter(self, src, period, now):
        s = self.cat.sources[src]
        col = s["time"]
        if s["kind"] == "snapshot":
            return None
        if s["time_type"] == "timestamp":
            return f"{col} >= '{period.start:%Y-%m-%d %H:%M:%S}' AND {col} < '{period.end:%Y-%m-%d %H:%M:%S}'"
        return f"{col} >= '{period.start_date}' AND {col} < '{period.end_date}'"

    def _grain_expr(self, src, grain):
        s = self.cat.sources[src]
        col = s["time"]
        if s["kind"] == "snapshot":
            return col, "date"
        if grain == "hour":
            return f"substr({col}, 12, 2) || ':00'", "hour"
        if grain == "day":
            return f"substr({col}, 1, 10)", "day"
        if grain == "week":
            return f"date({col}, 'weekday 0', '-6 days')", "week starting"
        if grain == "month":
            return f"substr({col}, 1, 7)", "month"
        if grain == "quarter":
            return f"substr({col}, 1, 4) || '-Q' || ((CAST(substr({col}, 6, 2) AS INTEGER) + 2) / 3)", "quarter"
        return f"substr({col}, 1, 4)", "year"

    def sql(self, q, now, period=None, label=None):
        """One SELECT for the query (period overrides q.period, label adds a period column)."""
        period = period or q.period
        met = self.cat.metrics[q.metric]
        src = met["source"]
        s = self.cat.sources[src]
        select, group_by, needs = [], [], set()
        if label:
            select.append(f"'{label}' AS period")
        for g in q.group:
            if g.startswith("grain:"):
                expr, name = self._grain_expr(src, g.split(":")[1])
            else:
                d = self.cat.dims[g]
                expr, name = d["expr"], d["label"].lower()
                needs.add(d.get("needs"))
            select.append(f"{expr} AS \"{name}\"")
            group_by.append(expr)
        select.append(f"{met['value']} AS {q.metric}")
        where = []
        if met.get("where"):
            where.append(met["where"])
        tf = self._time_filter(src, period, now)
        if tf:
            where.append(tf)
        elif s["kind"] == "snapshot":
            if any(g.startswith("grain:") for g in q.group):
                where.append(f"{s['time']} >= '{period.start_date}' AND {s['time']} < '{period.end_date}'")
            else:
                where.append(f"{s['time']} = '{self.snapshot_date(period, now)}'")
        for dim, vals in q.filters.items():
            d = self.cat.dims[dim]
            needs.add(d.get("needs"))
            if len(vals) == 1:
                where.append(f"{d['expr']} = '{vals[0]}'")
            else:
                where.append(f"{d['expr']} IN (" + ", ".join(f"'{v}'" for v in vals) + ")")
        joins = []
        if "b" in needs:
            needs.add("c")
        for key in ("c", "b"):
            if key in needs and key in s.get("joins", {}):
                joins.append(s["joins"][key])
        lines = ["SELECT " + ",\n       ".join(select), f"FROM {s['from']}"] + joins
        if where:
            lines.append("WHERE " + "\n  AND ".join(where))
        if group_by:
            lines.append("GROUP BY " + ", ".join(group_by))
            if any(g.startswith("grain:") for g in q.group):
                lines.append("ORDER BY " + ", ".join(group_by))
            elif not label:
                lines.append(f"ORDER BY {q.metric} {q.order.upper()}")
                if q.limit:
                    lines.append(f"LIMIT {q.limit}")
        return "\n".join(lines)

    def full_sql(self, q, now):
        if not q.compare:
            return self.sql(q, now), None
        snapshot = self.cat.sources[self.cat.metrics[q.metric]["source"]]["kind"] == "snapshot"
        if not snapshot:
            prev, prev_label = previous(q.period, q.yoy)
        else:
            cur_d = self.snapshot_date(q.period, now)
            if q.yoy:
                # the latest snapshot on or before the same day a year earlier
                target = add_months(cur_d, -12)
                month_end = add_months(target.replace(day=1), 1) - dt.timedelta(days=1)
                prev_d = target if target == month_end else target.replace(day=1) - dt.timedelta(days=1)
            else:
                prev_d = cur_d.replace(day=1) - dt.timedelta(days=1)
            prev = Period(dt.datetime.combine(prev_d, dt.time()), dt.datetime.combine(prev_d, dt.time()) + dt.timedelta(days=1), "")
            prev_label = f"{prev_d:%-d %B %Y}"
        a = self.sql(q, now, label="current")
        b = self.sql(q, now, period=prev, label="previous")
        tail = ""
        if q.group:
            tail = "\nORDER BY 1"
        return f"{a}\nUNION ALL\n{b}{tail}", (prev, prev_label)

    # ------------------------------------------------------------ answering
    def answer(self, question, conn, now, previous_query=None):
        q, early = self.parse(question, now, previous_query)
        if early:
            return early, None
        sql, prev = self.full_sql(q, now)
        try:
            df, truncated = guard.run(conn, sql)
        except Exception as e:  # pragma: no cover - shown to the user
            return Answer(f"The query failed: {e}", sql=sql, ok=False), q
        text, data, chart = self.describe(q, df, now, prev)
        return Answer(text, sql=sql, data=data, chart=chart, understood=self.understood(q, now)), q

    def understood(self, q, now):
        met = self.cat.metrics[q.metric]
        snapshot = self.cat.sources[met["source"]]["kind"] == "snapshot"
        out = [f"**KPI:** {met['label']} — {met['definition']}"]
        if snapshot and not any(g.startswith("grain:") for g in q.group):
            out.append(f"**On:** {self.snapshot_date(q.period, now):%-d %B %Y}"
                       + ("" if q.period.explicit else " (latest; you didn't name a date)"))
        else:
            out.append(f"**Period:** {q.period.label} ({q.period.dates()})"
                       + ("" if q.period.explicit else " — default, you didn't name one"))
        if q.group:
            names = [g.split(":")[1] if g.startswith("grain:") else self.cat.dims[g]["label"].lower() for g in q.group]
            out.append("**Split by:** " + ", ".join(names))
        if q.filters:
            out.append("**Only:** " + "; ".join(f"{self.cat.dims[d]['label'].lower()} = {', '.join(v)}"
                                                for d, v in q.filters.items()))
        if q.compare:
            out.append("**Compared with:** " + ("the same period a year earlier" if q.yoy else "the period before"))
        if q.ignored:
            out.append("**Words I didn't use:** " + ", ".join(q.ignored))
        out += [f"*{n}*" for n in q.notes]
        return out

    def describe(self, q, df, now, prev):
        met = self.cat.metrics[q.metric]
        unit = met["unit"]
        label = met["label"]
        suffix = (" for " + " / ".join(", ".join(v) for v in q.filters.values())) if q.filters else ""
        label_l = lc(label) + suffix
        label += suffix
        snapshot = self.cat.sources[met["source"]]["kind"] == "snapshot"
        when = (f"on {self.snapshot_date(q.period, now):%-d %B %Y}" if snapshot else q.period.label)
        if snapshot and self.snapshot_date(q.period, now) == now.date():
            when = "today"
        col = q.metric
        if df.empty or (len(df) == 1 and pd.isna(df[col].iloc[0])):
            return f"No data for {label_l} {when}.", df, None
        grain = next((g.split(":")[1] for g in q.group if g.startswith("grain:")), None)
        dims = [g for g in q.group if not g.startswith("grain:")]
        is_past = not q.period.partial

        if q.compare and prev is not None:
            return self._describe_compare(q, df, label, unit, when, prev[1], dims)
        if not q.group:
            v = df[col].iloc[0]
            return f"{label} {_when(when)}: **{fmt(v, unit)}**.", df, None
        if grain == "hour" and not dims:
            x = df.columns[0]
            peak = df.loc[df[col].idxmax()]
            total = df[col].sum() if unit in ("eur", "count") else None
            text = f"Busiest hour {_when(when)}: **{peak[x]}** ({fmt(peak[col], unit)})."
            if total is not None:
                text = f"{label} {_when(when)}: **{fmt(total, unit)}** in total. " + text
            if q.period.partial:
                text += f" The current hour is still running."
            return text, df, {"kind": "bar", "x": x, "y": col}
        if grain and not dims:
            x = df.columns[0]
            # a period that is still running (this month so far) would distort the comparison: leave it out
            running = q.period.partial and grain in ("month", "week", "day", "quarter", "year") and not snapshot \
                and len(df) > 2 and unit in ("eur", "count")
            full = df.iloc[:-1] if running else df
            first, last = full.iloc[0], full.iloc[-1]
            peak = full.loc[full[col].idxmax()]
            low = full.loc[full[col].idxmin()]
            change = _change(last[col], first[col], unit)
            text = (f"{label} went from **{fmt(first[col], unit)}** ({_lab(first[x], grain)}) to "
                    f"**{fmt(last[col], unit)}** ({_lab(last[x], grain)}){change}. "
                    f"Highest: {_lab(peak[x], grain)} ({fmt(peak[col], unit)}); lowest: {_lab(low[x], grain)} "
                    f"({fmt(low[col], unit)}).")
            if running:
                text += f" This {grain} so far ({_lab(df.iloc[-1][x], grain)}, not over yet): {fmt(df.iloc[-1][col], unit)}."
            return text, df, {"kind": "line", "x": x, "y": col}
        if grain and dims:
            x, color = df.columns[0], df.columns[1]
            tot = df.groupby(color)[col].sum() if unit in ("eur", "count") else df.groupby(color)[col].mean()
            lead = tot.idxmax()
            return (f"{label} by {grain} and {color}, {when}. Largest overall: **{lead}**.", df,
                    {"kind": "line", "x": x, "y": col, "color": color})
        x = df.columns[0]
        df = df.dropna(subset=[col])
        if len(df) == 1 and len(dims) == 1:
            return f"{label} {_when(when)} ({x}: {df.iloc[0][x]}): **{fmt(df.iloc[0][col], unit)}**.", df, None
        top, bottom = df.iloc[0], df.iloc[-1]
        if q.order == "asc":
            top, bottom = bottom, top
        parts = [f"**{top[x]}** has the highest {label_l} {_when(when)} ({fmt(top[col], unit)}"]
        additive = met["value"].startswith(("SUM(", "COUNT(*)")) or (met["value"].startswith("COUNT(DISTINCT") and x in
                                                                    ("branch", "city", "region", "segment", "age band"))
        if additive and len(df) > 1 and df[col].sum() > 0:
            parts[0] += f", {100 * top[col] / df[col].sum():.0f}% of the total"
        parts[0] += ")"
        if len(df) > 1:
            parts.append(f"**{bottom[x]}** the lowest ({fmt(bottom[col], unit)})")
        if len(dims) == 2:
            text = f"{label} {_when(when)}, by {dims[0]} and {dims[1]}. Highest: **{top[x]} / {top[df.columns[1]]}** " \
                   f"({fmt(top[col], unit)})."
            return text, df, {"kind": "bar", "x": x, "y": col, "color": df.columns[1]}
        if q.order == "asc" and q.limit:
            parts = [f"Lowest {label_l} {_when(when)}: " + ", ".join(
                f"**{r[x]}** ({fmt(r[col], unit)})" for _, r in df.iterrows())]
        elif q.limit:
            parts = [f"Top {q.limit} by {label_l} {_when(when)}: " + ", ".join(
                f"**{r[x]}** ({fmt(r[col], unit)})" for _, r in df.iterrows())]
        text = "; ".join(parts) + "."
        return text, df, {"kind": "bar", "x": x, "y": col}

    def _describe_compare(self, q, df, label, unit, when, prev_label, dims):
        col = q.metric
        cur = df[df["period"] == "current"].drop(columns="period")
        old = df[df["period"] == "previous"].drop(columns="period")
        if not dims:
            a = cur[col].iloc[0] if len(cur) else None
            b = old[col].iloc[0] if len(old) else None
            if a is None or pd.isna(a):
                return f"No data for {label} {_when(when)}.", df, None
            text = f"{label} {_when(when)} is **{fmt(a, unit)}**"
            if b is None or pd.isna(b) or b == 0:
                text += f"; there is no data for {prev_label} to compare with."
            else:
                text += f", {_diff_words(a, b, unit)} {prev_label} ({fmt(b, unit)})."
            short = re.sub(r"\s*\(.*\)$", "", prev_label)
            table = pd.DataFrame({"period": [_when(when), short], label: [a, b]})
            return text, table, {"kind": "bar", "x": "period", "y": label, "sort": False}
        x = cur.columns[0]
        m = cur.merge(old, on=x, how="outer", suffixes=("", "_before"))
        m["change_%"] = (100 * (m[col] - m[f"{col}_before"]) / m[f"{col}_before"].abs()).round(1)
        m = m.sort_values("change_%", ascending=False).reset_index(drop=True)
        up, down = m.iloc[0], m.iloc[-1]
        hi_word = "biggest rise" if up["change_%"] > 0 else "smallest fall"
        lo_word = "biggest fall" if down["change_%"] < 0 else "smallest rise"
        text = (f"{label} by {x} {_when(when)} vs {prev_label}: the {hi_word} is **{up[x]}** "
                f"({_pct(up['change_%'])}, now {fmt(up[col], unit)}) and the {lo_word} **{down[x]}** "
                f"({_pct(down['change_%'])}, now {fmt(down[col], unit)}).")
        return text, m, {"kind": "bar", "x": x, "y": "change_%"}


def _when(when):
    return when if when.startswith(("on ", "today", "in ", "the ", "this ", "last ", "since ", "yesterday")) \
        else f"in {when}" if when[:1].isdigit() or when[:1].isupper() else when


def _lab(v, grain):
    if isinstance(v, str) and len(v) == 7 and v[4] == "-" and v[5:].isdigit():
        return dt.date(int(v[:4]), int(v[5:]), 1).strftime("%b %Y")
    if isinstance(v, str) and len(v) == 10 and v[4] == "-":
        d = dt.date.fromisoformat(v)
        if grain == "week":
            return f"week of {d:%-d %b}"
        month_end = (d + dt.timedelta(days=1)).day == 1
        return d.strftime("%b %Y") if month_end and grain != "day" else d.strftime("%-d %b %Y")
    return str(v)


def lc(label):
    """Lower-case a KPI label but keep acronyms (ATM, NPL)."""
    return " ".join(w if w.isupper() and len(w) > 1 else w.lower() for w in label.split())


def _change(new, old, unit):
    if old in (None, 0) or pd.isna(old):
        return ""
    if unit in ("pct", "rate"):
        return f" ({new - old:+.2f} points)"
    return f" ({100 * (new - old) / abs(old):+.0f}%)"


def _pct(v):
    return "n/a" if pd.isna(v) else f"{v:+.0f}%"


def _diff_words(a, b, unit):
    if unit in ("pct", "rate"):
        d = a - b
        return "unchanged from" if abs(d) < .005 else f"{'up' if d > 0 else 'down'} {abs(d):.2f} points on"
    ch = 100 * (a - b) / abs(b)
    return "about the same as" if abs(ch) < .5 else f"{'up' if ch > 0 else 'down'} {abs(ch):.1f}% on"


def fmt(v, unit):
    if v is None or pd.isna(v):
        return "–"
    if unit == "eur":
        a = abs(v)
        sign = "-" if v < 0 else ""
        if a >= 1e6:
            return f"{sign}€{a / 1e6:.2f}M"
        if a >= 1e4:
            return f"{sign}€{a / 1e3:.1f}k"
        if a >= 100:
            return f"{sign}€{a:,.0f}"
        return f"{sign}€{a:,.2f}"
    if unit == "pct":
        return f"{v:.1f}%" if abs(v) >= 10 else f"{v:.2f}%"
    if unit == "rate":
        return f"{v:.2f}%"
    return f"{v:,.0f}"
