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

from bank_assistant import ai, alerts, examples, guard
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


# ---------------------------------------------------------------- sidebar

with st.sidebar:
    st.markdown("### 🤖 Who answers")
    mode = st.radio("Answer engine", ["Built-in (free, no key)", "AI model (your API key)"],
                    help="Built-in: understands questions about the bank's KPIs and writes the SQL itself, "
                         "without any AI model. AI model: any question, using your own key.")
    llm = None
    if mode.startswith("AI"):
        provider = st.selectbox("Provider", list(ai.PROVIDERS))
        key = st.text_input("API key", type="password", placeholder="Used only for this session, never stored",
                            help="The key stays in this browser session and goes only to the provider you chose.")
        model = st.text_input("Model", value=ai.PROVIDERS[provider]["default_model"])
        base_url = None
        if provider.startswith("OpenAI-compatible"):
            base_url = st.text_input("Base URL", value=ai.PROVIDERS[provider]["base_url"])
        if key or provider.startswith("OpenAI-compatible"):
            llm = ai.LLM(provider, key, model, base_url)
            st.success("AI mode on. Saved examples still run without the model.")
        else:
            st.info("Paste a key to switch AI mode on. Until then, the built-in engine answers.")
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
    st.caption(f"Answered by: {badge}")


def answer(question):
    """Route a question: saved example, AI model, or the built-in engine."""
    if question in examples.BY_QUESTION:
        return examples.run(examples.BY_QUESTION[question], conn, now)
    if llm is not None:
        try:
            a = ai.ask(llm, question, conn, now, ss.ai_history)
        except ai.LLMError as e:
            return Answer(f"⚠️ {e}", ok=False, source="ai")
        if a.ok and a.sql:
            ss.ai_history.append((question, a.sql))
        return a
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

tab_ask, tab_alerts, tab_dash, tab_acc, tab_data, tab_how = st.tabs(
    ["💬 Ask", "🔔 Alerts", "📊 Dashboard", "🧪 Accuracy", "🗂️ Data & KPIs", "ℹ️ How it works"])

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
