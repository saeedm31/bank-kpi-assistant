"""Turn time words in a question ("last month", "since March", "Q2 2025") into exact date ranges."""

import calendar
import datetime as dt
import re
from dataclasses import dataclass, replace

NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
           "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "eighteen": 18, "twenty four": 24,
           "twenty-four": 24, "thirty": 30, "a": 1}
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9
MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
NUM_RE = r"\d+|" + "|".join(sorted(NUMBERS, key=len, reverse=True))


@dataclass(frozen=True)
class Period:
    start: dt.datetime          # inclusive
    end: dt.datetime            # exclusive
    label: str                  # e.g. "this month so far"
    unit: str = "day"           # calendar unit for comparisons: hour/day/week/month/quarter/year/span
    partial: bool = False       # the period is still running
    rolling: bool = False       # "last 30 days" style
    explicit: bool = True       # the user named it

    @property
    def start_date(self):
        return self.start.date()

    @property
    def end_date(self):
        """Exclusive end as a date (rounded up when the end is mid-day)."""
        d = self.end.date()
        return d if self.end.time() == dt.time(0) else d + dt.timedelta(days=1)

    def dates(self):
        last = self.end - dt.timedelta(seconds=1)
        a, b = self.start, last
        if a.date() == b.date():
            return f"{a:%-d %b %Y}" + (f", {a:%H:%M}-{self.end:%H:%M}" if self.unit in ("hour", "span") else "")
        if a.year == b.year:
            return f"{a:%-d %b} – {b:%-d %b %Y}"
        return f"{a:%-d %b %Y} – {b:%-d %b %Y}"


def _day(d):
    return dt.datetime(d.year, d.month, d.day)


def add_months(d, n):
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    day = min(d.day, calendar.monthrange(y, m + 1)[1])
    return d.replace(year=y, month=m + 1, day=day)


def month_period(year, month, now, label=None):
    start = dt.datetime(year, month, 1)
    end = add_months(start, 1)
    partial = start <= now < end
    return Period(start, min(end, now) if partial else end, label or f"{start:%B %Y}" + (" so far" if partial else ""),
                  "month", partial)


def this_unit(unit, now):
    today = _day(now)
    if unit == "day":
        return Period(today, now, "today so far", "day", True)
    if unit == "week":
        start = today - dt.timedelta(days=today.weekday())
        return Period(start, now, "this week so far", "week", True)
    if unit == "month":
        return Period(today.replace(day=1), now, "this month so far", "month", True)
    if unit == "quarter":
        start = today.replace(month=3 * ((today.month - 1) // 3) + 1, day=1)
        return Period(start, now, "this quarter so far", "quarter", True)
    if unit == "year":
        return Period(today.replace(month=1, day=1), now, "this year so far", "year", True)
    raise ValueError(unit)


def last_unit(unit, now):
    cur = this_unit(unit, now)
    start = shift(cur, -1).start
    names = {"day": "yesterday", "week": "last week", "month": "last month", "quarter": "last quarter", "year": "last year"}
    label = names[unit]
    if unit == "month":
        label = f"{start:%B %Y}"
    elif unit == "year":
        label = f"{start:%Y}"
    return Period(start, cur.start, label, unit, False)


def shift(p, n, unit=None):
    """Move a period by n units (keeps its length: a partial month compares with the same days)."""
    unit = unit or p.unit
    if unit in ("month", "quarter", "year"):
        k = {"month": 1, "quarter": 3, "year": 12}[unit] * n
        return replace(p, start=add_months(p.start, k), end=add_months(p.end, k), explicit=False)
    days = {"hour": None, "day": 1, "week": 7, "span": None}[unit]
    delta = (p.end - p.start) * n if days is None else dt.timedelta(days=days * n)
    return replace(p, start=p.start + delta, end=p.end + delta, explicit=False)


def previous(p, year_on_year=False):
    """The period to compare with, and its label."""
    if year_on_year:
        q = shift(p, -1, "year")
        return replace(q, label="the same period last year" if p.partial or p.rolling else f"a year earlier"), \
            "the same period last year"
    if p.unit == "day" and p.partial:
        q = shift(p, -7, "day")
        return q, f"the same time last {p.start:%A} ({q.start:%-d %b})"
    q = shift(p, -1)
    if p.partial:
        names = {"week": "last week", "month": "last month", "quarter": "last quarter", "year": "last year", "day": "yesterday"}
        return q, f"the same days {names.get(p.unit, 'before')} ({q.dates()})"
    if p.rolling:
        return q, f"the {_span_words(p)} before ({q.dates()})"
    if p.unit == "month":
        return q, f"{q.start:%B %Y}"
    if p.unit == "year":
        return q, f"{q.start:%Y}"
    if p.unit == "quarter":
        return q, f"Q{(q.start.month - 1) // 3 + 1} {q.start:%Y}"
    return q, f"the period before ({q.dates()})"


def _span_words(p):
    hours = (p.end - p.start).total_seconds() / 3600
    if hours < 48:
        return f"{hours:.0f} hours"
    return f"{round(hours / 24)} days"


def _num(word):
    return int(word) if word.isdigit() else NUMBERS[word]


PATTERNS = []


def _pattern(regex):
    def deco(f):
        PATTERNS.append((re.compile(regex), f))
        return f
    return deco


@_pattern(rf"\b(?:in |over |during |for |within )?(?:the )?(?:last|past|previous) ({NUM_RE}) (hours?|days?|weeks?|months?|quarters?|years?)\b")
def _last_n(m, now):
    n, unit = _num(m.group(1)), m.group(2).rstrip("s")
    if unit == "hour":
        return Period(now - dt.timedelta(hours=n), now, f"the last {n} hours", "span", True, True)
    if unit == "day":
        return Period(now - dt.timedelta(days=n), now, f"the last {n} days", "span", True, True)
    if unit == "week":
        return Period(now - dt.timedelta(weeks=n), now, f"the last {n} weeks", "span", True, True)
    months = {"month": n, "quarter": 3 * n, "year": 12 * n}[unit]
    start = add_months(_day(now).replace(day=1), -(months - 1))
    return Period(start, now, f"the last {months} months", "span", True, True)


@_pattern(r"\b(?:in |over |during |for |within )?(?:the )?past (hour|day|week|month|year)\b")
def _past_unit(m, now):
    unit = m.group(1)
    delta = {"hour": dt.timedelta(hours=1), "day": dt.timedelta(hours=24), "week": dt.timedelta(days=7),
             "month": dt.timedelta(days=30), "year": dt.timedelta(days=365)}[unit]
    words = {"hour": "the last hour", "day": "the last 24 hours", "week": "the last 7 days",
             "month": "the last 30 days", "year": "the last 12 months"}[unit]
    if unit == "year":
        return Period(add_months(_day(now).replace(day=1), -11), now, words, "span", True, True)
    return Period(now - delta, now, words, "span", True, True)


@_pattern(r"\b(?:in |over |during |for |within )?(?:the )?last (hour)\b")
def _last_hour(m, now):
    return Period(now - dt.timedelta(hours=1), now, "the last hour", "span", True, True)


@_pattern(r"\b(?:in |during |for |over )?(?:the )?(?:last|previous|prior) (week|month|quarter|year)\b")
def _last_cal(m, now):
    return last_unit(m.group(1), now)


@_pattern(r"\b(?:so far )?(?:in |during |for |over )?(?:this|the current) (week|month|quarter|year)(?: so far| to date)?\b")
def _this_cal(m, now):
    return this_unit(m.group(1), now)


@_pattern(r"\b(week|month|quarter|year)[- ]to[- ]date\b|\b(wtd|mtd|qtd|ytd)\b")
def _to_date(m, now):
    unit = m.group(1) or {"wtd": "week", "mtd": "month", "qtd": "quarter", "ytd": "year"}[m.group(2)]
    return this_unit(unit, now)


@_pattern(r"\b(?:so far )?today(?: so far)?\b")
def _today(m, now):
    return this_unit("day", now)


@_pattern(r"\byesterday\b")
def _yesterday(m, now):
    return last_unit("day", now)


@_pattern(rf"\bsince (?:the (?:start|beginning) of )?({MONTH_RE})(?: (\d{{4}}))?\b")
def _since_month(m, now):
    month = MONTHS[m.group(1)]
    year = int(m.group(2)) if m.group(2) else (now.year if month <= now.month else now.year - 1)
    start = dt.datetime(year, month, 1)
    return Period(start, now, f"since {start:%B %Y}", "span", True)


@_pattern(r"\bsince (?:the (?:start|beginning) of )?(20\d\d)\b")
def _since_year(m, now):
    start = dt.datetime(int(m.group(1)), 1, 1)
    return Period(start, now, f"since {start:%Y}", "span", True)


@_pattern(r"\b(?:in |during |for )?(?:q([1-4])|(first|second|third|fourth) quarter)(?: of)?(?: (20\d\d))?\b")
def _quarter(m, now):
    q = int(m.group(1)) if m.group(1) else ["first", "second", "third", "fourth"].index(m.group(2)) + 1
    year = int(m.group(3)) if m.group(3) else now.year
    start = dt.datetime(year, 3 * q - 2, 1)
    if start > now and not m.group(3):
        start = start.replace(year=year - 1)
    end = add_months(start, 3)
    partial = start <= now < end
    return Period(start, min(end, now), f"Q{q} {start:%Y}" + (" so far" if partial else ""), "quarter", partial)


@_pattern(rf"\b(?:(?:in|of|during|for|since) )?(?:the )?(?:(?:start|beginning|end) of )?({MONTH_RE})(?: (20\d\d))?\b")
def _month(m, now):
    word = m.group(1)
    text = m.group(0)
    if word == "may" and not (m.group(2) or re.match(r"(in|of|during|for|since) ", text)):
        return None  # "may" the verb
    month = MONTHS[word]
    year = int(m.group(2)) if m.group(2) else (now.year if month <= now.month else now.year - 1)
    return month_period(year, month, now)


@_pattern(r"\b(?:in |during |for |of )?(?:the )?(?:end of |year )?(20\d\d)\b")
def _year(m, now):
    year = int(m.group(1))
    start = dt.datetime(year, 1, 1)
    end = dt.datetime(year + 1, 1, 1)
    partial = start <= now < end
    return Period(start, min(end, now), f"{year}" + (" so far" if partial else ""), "year", partial)


SEASONS = {"spring": (3, 5), "summer": (6, 8), "autumn": (9, 11), "fall": (9, 11), "winter": (12, 2)}


@_pattern(r"\b(?:in |during |over |for )?(?:the |this |last )?(spring|summer|autumn|fall|winter)(?: (20\d\d))?\b")
def _season(m, now):
    name = m.group(1)
    first, last = SEASONS[name]
    if m.group(2):
        year = int(m.group(2))
    else:
        year = now.year if dt.datetime(now.year, first, 1) <= now else now.year - 1
        if "last " in m.group(0) and dt.datetime(year, first, 1) <= now:
            year -= 1
    start = dt.datetime(year, first, 1)
    end = add_months(start, 3)
    partial = start <= now < end
    label = f"{name} {year}" + (f"/{str(year + 1)[2:]}" if name == "winter" else "")
    return Period(start, min(end, now), label + (" so far" if partial else ""), "span", partial)


@_pattern(r"\b(?:of )?all time\b|\bever\b|\bsince (?:the )?(?:start|beginning)\b|\bin total\b")
def _all_time(m, now):
    return Period(dt.datetime(2000, 1, 1), now, "the whole history", "span", True)


@_pattern(r"\b(?:right now|now|currently|at the moment|as of today|latest|current(?! accounts?\b))\b")
def _now(m, now):
    return Period(_day(now), now, "now", "now", True, explicit=True)


def find_period(text, now):
    """Return (Period or None, text with the time words blanked out).

    When several time phrases appear, the first one in the sentence is the period asked about
    ("this month compared with last month")."""
    best = None
    for order, (regex, f) in enumerate(PATTERNS):
        for m in regex.finditer(text):
            p = f(m, now)
            if p is None:
                continue
            key = (m.start(), order)
            if best is None or key < best[0]:
                best = (key, p, m)
            break
    if best is None:
        return None, text
    _, p, m = best
    return p, text[:m.start()] + " " * (m.end() - m.start()) + text[m.end():]
