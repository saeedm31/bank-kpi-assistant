"""Bank KPI Assistant: talk with your database.

Ask a question in plain English; the assistant turns it into SQL, runs it safely on
live (simulated) bank data, and answers with the number, a chart and its working.

Run:  streamlit run app.py
"""

import datetime as dt
import json
import os

import altair as alt
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from bank_assistant import ai, alerts, costs, examples, guard, usage
from bank_assistant.db import TABLES, Bank
from bank_assistant.engine import Answer, Catalog, Engine, fmt
from bank_assistant.simulate import lisbon_now

HERE = os.path.dirname(os.path.abspath(__file__))
st.set_page_config(page_title="Bank KPI Assistant", page_icon="🏦", layout="wide")

BUILT_IN_EXAMPLES = [
    "What is card spend this month?",
    "Card spend by branch last month",
    "NPL ratio by region",
    "Net inflow by month",
    "Late payments on personal loans by region",
    "Card spend by category year on year",
    "Digital share of transfers by age band this year",
    "Total deposits over time",
    "Card spend today by hour",
    "How many new customers this year vs last year?",
]


# ---------------------------------------------------------------- data

@st.cache_resource(max_entries=1, show_spinner=False)
def open_bank(day):
    """One simulated bank per day, shared by all visitors. New payments are added as time passes."""
    bank = Bank()
    return bank, Engine(bank.connect())


def connection(bank):
    key = f"conn::{bank.path}"
    if key not in st.session_state:
        st.session_state[key] = bank.connect()
    return st.session_state[key]


with st.spinner("Opening the bank: simulating two years of payments (about 15 seconds on the first visit of the day)…"):
    bank, engine = open_bank(lisbon_now().date().isoformat())
bank.catch_up()
conn = connection(bank)
now = bank.now
ss = st.session_state
ss.setdefault("messages", [])
ss.setdefault("last_query", None)
ss.setdefault("ai_history", [])
today = now.date()

# usage ledger: read once from this browser's cookie, written back whenever it changes (end of page)
if "ledger" not in ss:
    try:
        raw = st.context.cookies.get(usage.COOKIE)
    except Exception:
        raw = None
    ss.ledger = usage.decode(raw, today) if raw else usage.empty(today)
    ss.ledger_saved = raw
ss.setdefault("session_calls", [])       # every AI call in this session, for the log and CSV
ss.setdefault("session_free", 0)
ss.setdefault("cache_clock", {})         # demo mode: when the instructions were last "cached"
ss.setdefault("demo_prev", None)
ss.setdefault("fx", costs.USD_TO_EUR)
ss.setdefault("prices", dict(costs.PRICES))
ss.setdefault("caching", True)


# ---------------------------------------------------------------- sidebar

with st.sidebar:
    st.markdown("### 🤖 Who answers")
    mode = st.radio("Answer engine", ["Built-in (free, no key)", "AI model (your API key)"],
                    help="Built-in: understands questions about the bank's KPIs and writes the SQL itself, "
                         "without any AI model. AI model: any question, using your own key.")
    llm = None
    price_as = None
    if mode.startswith("AI"):
        provider = st.selectbox("Provider", list(ai.PROVIDERS),
                                help="Demo: no key and no real model. The built-in engine answers, and the app counts "
                                     "the tokens a real model would use, so you can try the cost tracker.")
        if provider == ai.DEMO:
            price_as = st.selectbox("Price it as", list(ss.prices), index=0)
            llm = ai.DemoLLM(engine, conn, now, price_as, ss.caching, ss.cache_clock, ss.demo_prev)
            st.success("Demo AI on: answers come from the built-in engine; token counts are estimated.")
        else:
            key = st.text_input("API key", type="password", placeholder="Used only for this session, never stored",
                                help="The key stays in this browser session and goes only to the provider you chose.")
            model = st.text_input("Model", value=ai.PROVIDERS[provider]["default_model"])
            price_as = model
            base_url = None
            if provider.startswith("OpenAI-compatible"):
                base_url = st.text_input("Base URL", value=ai.PROVIDERS[provider]["base_url"])
            if key or provider.startswith("OpenAI-compatible"):
                llm = ai.LLM(provider, key, model, base_url, caching=ss.caching)
                st.success("AI mode on. Saved examples still run without the model.")
            else:
                st.info("Paste a key to switch AI mode on. Until then, the built-in engine answers.")
        free_model = provider.startswith("OpenAI-compatible") and llm is not None and costs.is_local(llm.base_url)
        if llm is not None and not free_model:
            preview = costs.total(ai.typical_question(price_as, ss.caching), ss.prices, ss.fx)["eur"]
            st.caption(f"About **{costs.eur(preview)}** per question with {price_as}"
                       + ("" if preview is not None else " (add its price in 💶 Usage)") + ".")

    budget = usage.status(ss.ledger, today)
    st.markdown("### 💶 AI spend")
    st.progress(min(budget["share"], 1.0),
                text=f"{costs.eur(budget['spent'])} of {costs.eur(budget['budget'])} this month")
    if budget["level"] == "over" and llm is not None:
        llm = None
        st.error("Monthly AI budget reached, so AI mode is paused and the built-in engine answers. "
                 "Raise the budget or reset the counter in 💶 Usage.")
    elif budget["level"] == "warn":
        st.warning(f"{budget['share']:.0%} of this month's AI budget is used.")
    st.markdown("### 🕒 Live data")
    st.caption(f"Data up to **{now:%H:%M:%S}** Lisbon time, {now:%A %-d %B %Y}. "
               "New payments keep arriving; refresh to pull them in.")
    if st.button("Refresh data", width="stretch"):
        st.rerun()
    if st.button("Clear the chat", width="stretch"):
        ss.messages, ss.last_query, ss.ai_history = [], None, []
        st.rerun()
    st.markdown("---")
    st.caption("All data is simulated: a small Portuguese bank with 2,000 customers, 8 branches and two years of "
               "payments and loans. No real customer data.")


# ---------------------------------------------------------------- helpers

def chart(df, spec):
    if df is None or df.empty or not spec:
        return None
    x, y, color = spec["x"], spec["y"], spec.get("color")
    data = df.copy()
    data[x] = data[x].astype(str)
    if spec["kind"] == "line":
        base = alt.Chart(data).mark_line(point=True, strokeWidth=2.5)
        enc = dict(x=alt.X(f"{x}:O", title=None, axis=alt.Axis(labelAngle=-45 if len(data) > 8 else 0)),
                   y=alt.Y(f"{y}:Q", title=None), tooltip=list(data.columns))
    else:
        base = alt.Chart(data).mark_bar(cornerRadiusEnd=3)
        keep_order = color or spec.get("sort") is False
        enc = dict(y=alt.Y(f"{x}:N", sort=None if keep_order else "-x", title=None),
                   x=alt.X(f"{y}:Q", title=None), tooltip=list(data.columns))
    if color:
        enc["color"] = alt.Color(f"{color}:N", title=None)
    return base.encode(**enc).properties(height=min(60 + 28 * data[x].nunique(), 380) if spec["kind"] == "bar" else 300)


def auto_spec(df):
    """A chart for AI and saved answers: line over time, bars for categories."""
    if df is None or df.empty or len(df.columns) < 2 or len(df) < 2 or len(df) > 60:
        return None
    x = df.columns[0]
    nums = [c for c in df.columns[1:] if pd.api.types.is_numeric_dtype(df[c])]
    if not nums:
        return None
    sample = str(df[x].iloc[0])
    timeish = len(sample) in (7, 10) and sample[:2] in ("19", "20") and sample[4:5] == "-"
    return {"kind": "line" if timeish else "bar", "x": x, "y": nums[-1]}


def show(ans):
    st.markdown(ans.text)
    spec = ans.chart or (auto_spec(ans.data) if ans.source != "built-in" else None)
    c = chart(ans.data, spec)
    if c is not None:
        st.altair_chart(c, width="stretch")
    if ans.sql or ans.understood:
        cols = st.columns(3)
        with cols[0].popover("🧾 SQL", width="stretch"):
            st.code(ans.sql or "-- no query", language="sql")
        with cols[1].popover("📋 Data", width="stretch"):
            if ans.data is not None:
                st.dataframe(ans.data, hide_index=True, width="stretch")
        with cols[2].popover("🔎 How I read it", width="stretch"):
            for line in ans.understood or []:
                st.markdown(f"- {line}")
    badge = {"built-in": "Built-in engine", "saved": "Saved example", "ai": "AI model"}[ans.source]
    calls = getattr(ans, "usage", None)
    if calls:
        t = costs.total(calls, ss.prices, ss.fx)
        cached = t["cache_read"]
        tokens_in = t["input"] + t["cache_read"] + t["cache_write"]
        st.caption(f"Answered by: {badge} · 🔢 {t['calls']} model call{'s' if t['calls'] != 1 else ''}, "
                   f"{tokens_in:,} tokens in" + (f" ({cached:,} from cache)" if cached else "")
                   + (f" ({t['cache_write']:,} written to cache)" if t["cache_write"] else "")
                   + f", {t['output']:,} out · ≈ {costs.eur(t['eur'])}" + (" (estimated)" if t["estimated"] else ""))
    else:
        st.caption(f"Answered by: {badge} · no AI used, €0")


def answer(question):
    """Route a question: saved example (free), AI model, or the built-in engine (free)."""
    if question in examples.BY_QUESTION:
        usage.record_free(ss.ledger)
        ss.session_free += 1
        return examples.run(examples.BY_QUESTION[question], conn, now)
    if llm is not None:
        start = len(llm.calls)
        try:
            a = ai.ask(llm, question, conn, now, ss.ai_history)
        except ai.LLMError as e:
            a = ai.with_usage(Answer(f"⚠️ {e}", ok=False, source="ai"), llm.calls[start:])
        if a.ok and a.sql:
            ss.ai_history.append((question, a.sql))
        if isinstance(llm, ai.DemoLLM):
            ss.demo_prev = llm.previous
        if a.usage:
            summary = costs.total(a.usage, ss.prices, ss.fx)
            usage.record(ss.ledger, summary, a.usage[0].price_as or a.usage[0].model, question, today)
            for u in a.usage:
                ss.session_calls.append({"time": f"{now:%H:%M:%S}", "question": question, "model": u.model,
                                         "tokens in": u.input, "from cache": u.cache_read,
                                         "written to cache": u.cache_write, "tokens out": u.output,
                                         "eur": costs.cost_eur(u, ss.prices, ss.fx), "estimated": u.estimated})
        return a
    usage.record_free(ss.ledger)
    ss.session_free += 1
    a, q = engine.answer(question, conn, now, ss.last_query)
    if q is not None:
        ss.last_query = q
    return a


def headline():
    """The KPI row, built from the same definitions as the chat."""
    def pair(question):
        q, _ = engine.parse(question, now)
        sql, _ = engine.full_sql(q, now)
        df, _ = guard.run(conn, sql)
        cur = df.loc[df["period"] == "current", q.metric]
        old = df.loc[df["period"] == "previous", q.metric]
        return (cur.iloc[0] if len(cur) else None), (old.iloc[0] if len(old) else None)

    tiles = [("Total deposits", "Total deposits vs last month", "eur", "vs end of last month"),
             ("Loan book", "Loan book vs last month", "eur", "vs end of last month"),
             ("NPL ratio", "NPL ratio vs last month", "pct", "vs end of last month"),
             ("Card spend today", "Card spend today vs last week", "eur", "vs same time last week"),
             ("Active customers (month)", "Active customers this month vs last month", "count", "vs same days last month")]
    cols = st.columns(len(tiles))
    for col, (label, q, unit, note) in zip(cols, tiles):
        cur, old = pair(q)
        if cur is None or pd.isna(cur):
            col.metric(label, "–")
            continue
        if old is None or pd.isna(old) or old == 0:
            delta = None
        elif unit == "pct":
            delta = f"{cur - old:+.2f} pts"
        else:
            delta = f"{100 * (cur - old) / abs(old):+.1f}%"
        col.metric(label, fmt(cur, unit), delta, delta_color="inverse" if unit == "pct" else "normal", help=note)


# ---------------------------------------------------------------- page

st.title("🏦 Bank KPI Assistant")
st.caption("Ask about a bank's numbers in plain English. The assistant turns your question into SQL, runs it safely on "
           "live data, and answers with the number, a chart and its working. Data is simulated.")
headline()

tab_ask, tab_usage, tab_alerts, tab_dash, tab_acc, tab_data, tab_how = st.tabs(
    ["💬 Ask", "💶 Usage", "🔔 Alerts", "📊 Dashboard", "🧪 Accuracy", "🗂️ Data & KPIs", "ℹ️ How it works"])

with tab_ask:
    pending = None
    st.markdown("**Try a question**" + (" (sent to your AI model)" if llm else " (built-in engine, no key needed)"))
    cols = st.columns(5)
    for i, q in enumerate(BUILT_IN_EXAMPLES):
        if cols[i % 5].button(q, key=f"ex{i}", width="stretch"):
            pending = q
    st.markdown("**Harder questions** (several tables or customer-level logic; SQL written by hand, runs live)")
    cols = st.columns(3)
    for i, e in enumerate(examples.EXAMPLES):
        if cols[i % 3].button(e["question"], key=f"sv{i}", width="stretch"):
            pending = e["question"]

    chat = st.container()          # the conversation sits above the input box
    typed = st.chat_input("Ask about deposits, loans, card spend, customers… e.g. 'card spend in Porto vs last month'")
    with chat:
        for m in ss.messages:
            with st.chat_message(m["role"], avatar="🧑‍💼" if m["role"] == "user" else "🏦"):
                if m["role"] == "user":
                    st.markdown(m["text"])
                else:
                    show(m["answer"])
        question = typed or pending
        if question:
            ss.messages.append({"role": "user", "text": question})
            with st.chat_message("user", avatar="🧑‍💼"):
                st.markdown(question)
            with st.chat_message("assistant", avatar="🏦"):
                with st.spinner("Writing the query and checking the numbers…"):
                    a = answer(question)
                show(a)
            ss.messages.append({"role": "assistant", "answer": a})

with tab_usage:
    st.markdown("#### What the AI answers cost")
    st.caption("Token counts come from the provider's reply (Demo mode estimates them from the text). Euros are an "
               "estimate: list price per million tokens × tokens × the USD→EUR rate below. Your provider's bill is the "
               "real number. The counter is kept in this browser only (a small cookie); your API key is never stored.")
    led = ss.ledger
    s_eur = sum(r["eur"] or 0 for r in ss.session_calls)
    s_tok = sum(r["tokens in"] + r["from cache"] + r["written to cache"] + r["tokens out"] for r in ss.session_calls)
    s_q = len({(r["time"], r["question"]) for r in ss.session_calls})
    day, month, total_row = usage.day_row(led, today), usage.month_row(led, today), led["all"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("This session", costs.eur(s_eur), f"{s_q} AI question{'s' if s_q != 1 else ''} · {s_tok:,} tokens",
              delta_color="off")
    c2.metric("Today", costs.eur(day[5]), f"{day[0]} calls · {sum(day[1:5]):,} tokens", delta_color="off")
    c3.metric("This month", costs.eur(month[5]), f"{month[0]} calls · {sum(month[1:5]):,} tokens", delta_color="off")
    c4.metric(f"Since {dt.date.fromisoformat(led['since']):%-d %b}", costs.eur(total_row[5]),
              f"{led.get('asked', 0)} AI question{'s' if led.get('asked', 0) != 1 else ''}", delta_color="off")

    b = usage.status(led, today)
    st.progress(min(b["share"], 1.0), text=f"Monthly budget: {costs.eur(b['spent'])} of {costs.eur(b['budget'])} "
                                           f"({b['share']:.0%})")
    if b["level"] == "over":
        st.error("Budget reached: AI mode is paused until next month, a higher budget or a reset.")
    elif b["level"] == "warn":
        st.warning("Over 80% of the monthly budget is used.")

    avg = usage.avg_cost_per_question(led)
    if avg is None and price_as:
        avg = costs.total(ai.typical_question(price_as, ss.caching), ss.prices, ss.fx)["eur"]
    if led["free"]:
        saved = f" ≈ **{costs.eur(led['free'] * avg)}** not spent" if avg else ""
        st.markdown(f"🟢 **{led['free']}** questions were answered without AI (built-in engine or saved examples){saved}.")

    left, right = st.columns(2)
    with left:
        st.markdown("**Spend per day this month**")
        days = pd.DataFrame([{"day": f"{led['month']}-{d}", "eur": r[5], "calls": r[0]} for d, r in sorted(led["days"].items())])
        if days.empty:
            st.caption("No AI calls this month yet. Switch on AI mode (the Demo needs no key) and ask something.")
        else:
            st.altair_chart(alt.Chart(days).mark_bar(cornerRadiusEnd=3).encode(
                x=alt.X("day:O", title=None), y=alt.Y("eur:Q", title="€"), tooltip=["day", "calls", "eur"]),
                width="stretch")
        if led.get("prev"):
            st.caption(f"Last month ({led['prev']['month']}): {costs.eur(led['prev']['eur'])}")
    with right:
        st.markdown("**By model (since the last reset)**")
        models = pd.DataFrame([{"model": m, "calls": r[0], "tokens in": r[1] + r[3] + r[4], "from cache": r[3],
                                "tokens out": r[2], "€": round(r[5], 5)} for m, r in led["models"].items()])
        if models.empty:
            st.caption("Nothing yet.")
        else:
            st.dataframe(models, hide_index=True, width="stretch")
        if led["top"]:
            st.markdown("**Most expensive questions**")
            st.dataframe(pd.DataFrame(led["top"], columns=["€", "question"]), hide_index=True, width="stretch")

    st.markdown("**This session, call by call**")
    if ss.session_calls:
        log = pd.DataFrame(ss.session_calls)
        st.dataframe(log, hide_index=True, width="stretch")
        st.download_button("Download as CSV", log.to_csv(index=False), "ai_usage.csv", "text/csv")
    else:
        st.caption("No AI calls in this session yet.")

    with st.expander("⚙️ Budget, prices and reset"):
        new_budget = st.number_input("Monthly budget (€)", min_value=0.0, value=float(led["budget"]), step=0.5,
                                     help="At 80% you get a warning; at 100% AI mode pauses and the built-in "
                                          "engine answers. 0 means no limit.")
        if new_budget != led["budget"]:
            led["budget"] = new_budget
            st.rerun()
        ss.fx = st.number_input("1 US dollar in euros", min_value=0.1, max_value=5.0, value=float(ss.fx), step=0.01,
                                help=f"Default: the rate on 21 Oct 2026.")
        ss.caching = st.toggle("Prompt caching", value=ss.caching,
                               help="The long instructions (tables and KPI definitions) are sent with every question. "
                                    "With caching, repeat calls within 5 minutes read them at about 10% of the price.")
        st.caption(f"Prices in US dollars per million tokens, from the providers' price pages on {costs.PRICES_CHECKED}. "
                   "Edit them or add a model; local models (Ollama) cost nothing.")
        table = pd.DataFrame([{"model": m, "input": p[0], "cache write": p[1], "cache read": p[2], "output": p[3]}
                              for m, p in ss.prices.items()])
        edited = st.data_editor(table, hide_index=True, num_rows="dynamic", width="stretch", key="price_table")
        num = lambda v: float(v) if pd.notna(v) else 0.0
        ss.prices = {str(r["model"]).strip(): (num(r["input"]), num(r["cache write"]), num(r["cache read"]),
                                               num(r["output"]))
                     for r in edited.to_dict("records") if pd.notna(r.get("model")) and str(r["model"]).strip()}
        sure = st.checkbox("Yes, clear all saved usage in this browser")
        if st.button("Reset the counter", disabled=not sure):
            ss.ledger = usage.empty(today, budget=led["budget"])
            ss.session_calls, ss.session_free = [], 0
            st.rerun()

with tab_alerts:
    st.markdown("#### What's unusual today and yesterday")
    st.caption("Every branch is checked against the same weekday and hours of the last four weeks: card payments, "
               "ATM withdrawals and transfers out. An alert needs a big change that is very unlikely to be chance.")
    found = alerts.find(conn, now)
    if not found:
        st.success("Nothing unusual so far.")
    for i, a in enumerate(found):
        box = st.container(border=True)
        icon = "🔴" if a["severity"] == "high" else "🟠"
        box.markdown(f"{icon} **{a['title']}**  \n{a['text']}")
        if box.toggle("Show me", key=f"al{i}"):
            ans, _ = engine.answer(a["question"], conn, now)
            with box:
                show(ans)
    with st.expander("Behind the scenes: what the simulator planted"):
        st.caption("To test the alerts, the simulator adds one unusual event yesterday and one today. "
                   "The alerts above don't know about this list.")
        for e in bank.events:
            st.markdown(f"- **{e['date']}** from {e['starts'][11:]}: {e['what']}")

with tab_dash:
    st.caption("Each chart is one question to the built-in engine, so the dashboard and the chat always agree.")
    charts = ["Card spend by month", "Total deposits by month", "NPL ratio by month",
              "Digital share of transfers by month", "Card spend by branch this month", "New lending by loan type this year"]
    cols = st.columns(2)
    for i, q in enumerate(charts):
        with cols[i % 2].container(border=True):
            ans, _ = engine.answer(q, conn, now)
            st.markdown(f"**{q}**")
            c = chart(ans.data, ans.chart)
            if c is not None:
                st.altair_chart(c, width="stretch")
            st.caption(ans.text.replace("**", ""))

with tab_acc:
    st.markdown("#### How often is the built-in engine right?")
    st.caption("Test questions with hand-written correct SQL. An answer counts as right only when its numbers match. "
               "For questions outside its scope, the right answer is to say so.")

    def load(name):
        try:
            with open(os.path.join(HERE, "reports", name)) as f:
                return json.load(f)
        except OSError:
            return None

    main_set = load("eval_builtin_questions.json")
    unseen = load("eval_unseen_first_run.json")
    c1, c2, c3 = st.columns(3)
    if main_set:
        s = main_set["summary"]
        c1.metric("Main test set", f"{s['in_scope']['correct']}/{s['in_scope']['questions']} right",
                  help="40 questions used while building the engine.")
        c2.metric("Out of scope, declined", f"{s['out_of_scope']['declined']}/{s['out_of_scope']['questions']}",
                  help="Questions it should not answer: forecasts, 'why', actions, other topics.")
    if unseen:
        u = unseen["in_scope"]
        c3.metric("New wording, first try", f"{u['correct']}/{u['questions']} right",
                  f"{u['declined']} declined, {u['wrong']} wrong", delta_color="off",
                  help="20 questions written in advance and never used for tuning, scored once.")
        st.markdown(
            f"On questions it had never seen, the engine was right {u['correct']} times out of {u['questions']}, said "
            f"\"I can't answer that\" {u['declined']} times and was wrong {u['wrong']} times. A wrong answer is always "
            "visible in **How I read it** (for example, a filter it didn't apply). Those cases were then fixed. "
            "For wording it doesn't know, AI mode is the answer: run `python evaluate.py --ai openai` with your key "
            "to score a model on the same questions.")
    if main_set:
        with st.expander("All test questions and results"):
            st.dataframe(pd.DataFrame(main_set["questions"])[["question", "kind", "correct"]], hide_index=True,
                         width="stretch")

with tab_data:
    st.markdown("#### Tables")
    counts = {t: guard.run(conn, f"SELECT COUNT(*) FROM {t}")[0].iloc[0, 0] for t in TABLES}
    for t, (about, cols_) in TABLES.items():
        with st.expander(f"`{t}` — {about} ({counts[t]:,} rows)"):
            st.dataframe(pd.DataFrame({"column": list(cols_), "meaning": list(cols_.values())}), hide_index=True,
                         width="stretch")
            st.dataframe(guard.run(conn, f"SELECT * FROM {t} LIMIT 5")[0], hide_index=True, width="stretch")
    st.markdown("#### KPI definitions")
    st.caption("One agreed meaning for each number (`bank_assistant/kpis.yml`). The built-in engine and the AI model "
               "both use them, so the same question gives the same answer.")
    cat = Catalog()
    st.dataframe(pd.DataFrame([{"KPI": m["label"], "meaning": m["definition"],
                                "SQL": m["value"] + (f"  WHERE {m['where']}" if m.get("where") else "")}
                               for m in cat.metrics.values()]), hide_index=True, width="stretch")

with tab_how:
    st.markdown("""
#### From question to answer

1. **Understand.** The built-in engine finds the KPI, the period, the breakdown, filters and comparisons
   ("card spend by branch, last month, vs the month before"). In AI mode, a language model gets the schema
   and the KPI definitions and writes the SQL instead.
2. **Write SQL** from the agreed KPI definition, never an invented one.
3. **Run it safely.** Read-only connection, SELECT only, known tables only (an SQLite authorizer blocks
   writes, PRAGMA, ATTACH and extensions), a time limit and a row limit. If the AI's SQL fails, the error goes
   back to the model to fix, up to two times.
4. **Answer** in one or two sentences with the number, and show the chart, the SQL, the data and how the
   question was read, so anyone can check it.

#### What AI answers cost

Each AI answer shows the tokens it used (from the provider's reply) and an estimated price in euros. The
**💶 Usage** tab keeps totals in this browser, with a monthly budget that pauses AI mode when it is reached.
Prompt caching sends the long, unchanging instructions at about a tenth of the price on repeat calls.

#### Why a built-in engine and an AI model?

The built-in engine is free, instant and predictable, and says when it can't answer. The AI model handles
any wording and harder questions. Both use the same KPI definitions and the same safety checks.

#### Data

A simulated Portuguese retail bank: 8 branches, 2,000 customers, current and savings accounts, about
700,000 payments over two years and live up to the current minute, plus mortgages and other loans with
monthly snapshots. Patterns are built in to find: Christmas and summer peaks, the move from cash to cards
and from branches to the app, 14 salaries a year, customers whose salary stops before they leave, and late
payments rising on personal loans in the Algarve.
""")


# ---------------------------------------------------------------- save the usage ledger in this browser

_saved = usage.encode(ss.ledger)
if _saved != ss.get("ledger_saved"):
    components.html(
        "<script>document.cookie = '" + usage.COOKIE + "=" + _saved + "; path=/; max-age=31536000; SameSite=Lax'"
        " + (location.protocol === 'https:' ? '; Secure' : '');</script>", height=0)
    ss.ledger_saved = _saved
