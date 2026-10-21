"""AI mode: a language model writes the SQL, the app runs it safely, the model explains the result.

Works with Anthropic (Claude), OpenAI, or any OpenAI-compatible server (Ollama, OpenRouter, Groq...).
The key is only used for the calls below; the app never stores or logs it.

Loop:
1. The model gets the schema, the bank's KPI definitions and the time now, and replies with JSON:
   {"sql": "..."} or {"clarify": "a question back to the user"}.
2. The SQL goes through the guard (read-only, SELECT only, time limit). If it fails, the error goes
   back to the model to fix (up to 2 retries).
3. The model gets the result rows and writes a short answer that only uses those numbers.
"""

import json
import re

import requests

from . import guard
from .db import TABLES
from .engine import Answer, Catalog

PROVIDERS = {
    "Anthropic (Claude)": {"default_model": "claude-haiku-5-5", "base_url": "https://api.anthropic.com"},
    "OpenAI": {"default_model": "gpt-5-mini", "base_url": "https://api.openai.com/v1"},
    "OpenAI-compatible (Ollama, OpenRouter...)": {"default_model": "llama3.1", "base_url": "http://localhost:11434/v1"},
}


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, provider, api_key, model=None, base_url=None, timeout=90):
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown provider {provider}")
        self.provider = provider
        self.key = (api_key or "").strip()
        self.model = model or PROVIDERS[provider]["default_model"]
        self.base_url = (base_url or PROVIDERS[provider]["base_url"]).rstrip("/")
        self.timeout = timeout

    def complete(self, system, messages):
        try:
            if self.provider.startswith("Anthropic"):
                r = requests.post(f"{self.base_url}/v1/messages", timeout=self.timeout, json={
                    "model": self.model, "max_tokens": 1500, "system": system, "messages": messages},
                    headers={"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
                body = _json(r)
                return "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
            headers = {"Content-Type": "application/json"}
            if self.key:
                headers["Authorization"] = f"Bearer {self.key}"
            r = requests.post(f"{self.base_url}/chat/completions", timeout=self.timeout, headers=headers,
                              json={"model": self.model, "messages": [{"role": "system", "content": system}] + messages})
            body = _json(r)
            return body["choices"][0]["message"]["content"] or ""
        except requests.RequestException as e:
            raise LLMError(f"Could not reach the model: {type(e).__name__}") from e


def _json(r):
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400:
        err = body.get("error", {})
        msg = err.get("message") if isinstance(err, dict) else str(err)
        if r.status_code in (401, 403):
            raise LLMError("The API key was rejected. Check that it is correct and has credit.")
        raise LLMError(f"The model service returned an error ({r.status_code}): {(msg or '')[:200]}")
    return body


def schema_text():
    out = []
    for table, (about, cols) in TABLES.items():
        out.append(f"TABLE {table} -- {about}")
        for col, desc in cols.items():
            out.append(f"  {col}: {desc}")
    out.append("Keys: customers.branch_id -> branches; accounts.customer_id, transactions.customer_id, "
               "transactions.account_id, loans.customer_id -> their tables; balance_snapshots.account_id -> accounts; "
               "loan_snapshots.loan_id -> loans. Every table has its id column (customer_id, account_id, loan_id, txn_id).")
    return "\n".join(out)


def kpi_text(cat=None):
    cat = cat or Catalog()
    out = []
    for mid, m in cat.metrics.items():
        s = cat.sources[m["source"]]
        where = f" WHERE {m['where']}" if m.get("where") else ""
        out.append(f"- {m['label']}: {m['definition']} SQL: SELECT {m['value']} FROM {s['from']}{where} "
                   f"(time column: {s['time']}{', monthly snapshot: pick one snapshot_date' if s['kind'] == 'snapshot' else ''})")
    return "\n".join(out)


SYSTEM = """You are the data analyst of a retail bank. You answer questions by writing ONE SQLite query.

Database (SQLite; dates and times are TEXT):
{schema}

The bank's agreed KPI definitions. When a question uses one of these words, use exactly this definition:
{kpis}

Now is {now} (Lisbon time). Data runs up to now.
Rules:
- One SELECT statement only (WITH is fine). Never change data.
- "This month/week/year" means from its start until now ("so far"); "last month" is the previous full calendar month.
- Snapshot tables (balance_snapshots, loan_snapshots) have one row per account/loan per month end, and today's date
  for the current month. For a value "now", use snapshot_date = '{today}'. For a trend, group by snapshot_date.
- Compare timestamps as text, e.g. t.ts >= '2026-10-01 00:00:00'. Use date(), strftime() and substr() for periods.
- Give columns short readable names. Round money to 2 decimals and percentages to 1-2 decimals.
- Return at most 200 rows; aggregate when possible.
- If the question is truly ambiguous, or not about this bank's data, ask a short question back instead.

Reply with JSON only, no other text:
{{"sql": "SELECT ..."}}  or  {{"clarify": "your question to the user"}}"""

ANSWER = """You are a bank analyst writing for a busy manager. Answer the question in 1-3 short sentences of plain
English, using only the numbers in the query result below. Say the period the numbers cover. Format money with € and
k/M, keep percentages to one decimal. If the result is empty, say so. Do not mention SQL. Now is {now}."""


def parse_reply(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("The model did not reply with JSON.")
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        # some models put a raw newline inside the SQL string
        obj = json.loads(m.group(0).replace("\n", " "))
    if not isinstance(obj, dict) or not (obj.get("sql") or obj.get("clarify")):
        raise ValueError("The model's JSON has neither 'sql' nor 'clarify'.")
    return obj


def ask(llm, question, conn, now, history=(), retries=2):
    """Answer one question. `history` is a list of (question, sql) pairs for follow-ups."""
    system = SYSTEM.format(schema=schema_text(), kpis=kpi_text(), now=now.strftime("%Y-%m-%d %H:%M:%S (%A)"),
                           today=now.date().isoformat())
    messages = []
    for q, sql in list(history)[-3:]:
        messages += [{"role": "user", "content": q}, {"role": "assistant", "content": json.dumps({"sql": sql})}]
    messages.append({"role": "user", "content": question})
    steps = []
    sql = None
    for attempt in range(retries + 1):
        reply = llm.complete(system, messages)
        try:
            obj = parse_reply(reply)
        except ValueError as e:
            steps.append(f"Attempt {attempt + 1}: {e}")
            messages += [{"role": "assistant", "content": reply or "(empty)"},
                         {"role": "user", "content": f"{e} Reply with JSON only."}]
            continue
        if obj.get("clarify") and not obj.get("sql"):
            return Answer(obj["clarify"], source="ai", understood=["The model asked a question back."], ok=False)
        sql = obj["sql"]
        try:
            df, truncated = guard.run(conn, sql)
            break
        except Exception as e:
            steps.append(f"Attempt {attempt + 1} failed: {e}")
            messages += [{"role": "assistant", "content": json.dumps({"sql": sql})},
                         {"role": "user", "content": f"That query failed with: {e}. Fix it and reply with JSON only."}]
    else:
        return Answer("I couldn't get a working query for that. " + (steps[-1] if steps else ""), sql=sql or "",
                      source="ai", understood=steps, ok=False)

    csv = df.head(40).to_csv(index=False)
    note = f"\n(Showing 40 of {len(df)} rows.)" if len(df) > 40 else ""
    if truncated:
        note += f"\n(The result was cut at {len(df)} rows.)"
    text = llm.complete(ANSWER.format(now=now.strftime("%Y-%m-%d %H:%M")),
                        [{"role": "user", "content": f"Question: {question}\n\nQuery result (CSV):\n{csv}{note}"}])
    return Answer(text.strip() or "(The model returned no text.)", sql=guard.clean(sql), data=df, source="ai",
                  understood=steps + [f"Model: {llm.model}"])
