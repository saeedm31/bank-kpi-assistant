"""AI mode: a language model writes the SQL, the app runs it safely, the model explains the result.

Works with Anthropic (Claude), OpenAI, or any OpenAI-compatible server (Ollama, OpenRouter, Groq...).
The key is only used for the calls below; the app never stores or logs it.

Loop:
1. The model gets the schema, the bank's KPI definitions and the time now, and replies with JSON:
   {"sql": "..."} or {"clarify": "a question back to the user"}.
2. The SQL goes through the guard (read-only, SELECT only, time limit). If it fails, the error goes
   back to the model to fix (up to 2 retries).
3. The model gets the result rows and writes a short answer that only uses those numbers.

Every call's token counts are kept (llm.calls), so the app can show what each answer cost.
"""

import json
import re
import time

import requests

from . import costs, guard
from .db import TABLES
from .engine import Answer, Catalog

PROVIDERS = {
    "Demo (simulated AI, no key)": {"default_model": "claude-haiku-5-5", "base_url": ""},
    "Anthropic (Claude)": {"default_model": "claude-haiku-5-5", "base_url": "https://api.anthropic.com"},
    "OpenAI": {"default_model": "gpt-6-luna", "base_url": "https://api.openai.com/v1"},
    "OpenAI-compatible (Ollama, OpenRouter...)": {"default_model": "llama3.1", "base_url": "http://localhost:11434/v1"},
}
DEMO = "Demo (simulated AI, no key)"


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, provider, api_key, model=None, base_url=None, timeout=90, caching=True):
        if provider not in PROVIDERS or provider == DEMO:
            raise ValueError(f"Unknown provider {provider}")
        self.provider = provider
        self.key = (api_key or "").strip()
        self.model = model or PROVIDERS[provider]["default_model"]
        self.base_url = (base_url or PROVIDERS[provider]["base_url"]).rstrip("/")
        self.timeout = timeout
        self.caching = caching
        self.calls = []          # costs.Usage for every call made

    def complete(self, system, messages):
        try:
            if self.provider.startswith("Anthropic"):
                # Mark the long, unchanging instructions as cacheable: later calls read them at ~10% of the price.
                sys_block = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}] \
                    if self.caching and len(system) > 4000 else system
                r = requests.post(f"{self.base_url}/v1/messages", timeout=self.timeout, json={
                    "model": self.model, "max_tokens": 1500, "system": sys_block, "messages": messages},
                    headers={"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
                body = _json(r)
                text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
                usage = costs.from_anthropic(body, self.model)
            else:
                headers = {"Content-Type": "application/json"}
                if self.key:
                    headers["Authorization"] = f"Bearer {self.key}"
                r = requests.post(f"{self.base_url}/chat/completions", timeout=self.timeout, headers=headers,
                                  json={"model": self.model, "messages": [{"role": "system", "content": system}] + messages})
                body = _json(r)
                text = body["choices"][0]["message"]["content"] or ""
                usage = costs.from_openai(body, self.model, "openai" if self.provider == "OpenAI" else "compatible",
                                          free=costs.is_local(self.base_url))
            if usage.estimated:      # the server sent no counts: estimate from the text
                usage.input = costs.estimate_tokens(system + "".join(m["content"] for m in messages))
                usage.output = costs.estimate_tokens(text)
            self.calls.append(usage)
            return text
        except requests.RequestException as e:
            raise LLMError(f"Could not reach the model: {type(e).__name__}") from e


class DemoLLM:
    """No real model and no key: the built-in engine writes the SQL and the answer, while the tokens a real
    model would have used are estimated from the actual prompts and replies, priced as `price_as`.
    It shows how the cost tracker works; the counts are marked as estimated."""

    provider = DEMO

    def __init__(self, engine, conn, now, price_as="claude-haiku-5-5", caching=True, cache_clock=None,
                 previous=None):
        self.engine, self.conn, self.now = engine, conn, now
        self.price_as = price_as
        self.model = f"demo, priced as {price_as}"
        self.caching = caching
        self.cache_clock = cache_clock if cache_clock is not None else {}
        self.previous = previous
        self.calls = []
        self._text = None

    def complete(self, system, messages):
        last = messages[-1]["content"]
        if "Query result (CSV)" in last:
            out = self._text or "Here is the result."
            self._record(system, messages, out, cacheable=False)
            return out
        q, early = self.engine.parse(last, self.now, self.previous)
        if early is not None:
            out = json.dumps({"clarify": early.text.replace("**", "").replace("*", "")})
        else:
            sql, prev = self.engine.full_sql(q, self.now)
            df, _ = guard.run(self.conn, sql)
            text, _, _ = self.engine.describe(q, df, self.now, prev)
            self._text = text.replace("**", "")
            self.previous = q
            out = json.dumps({"sql": sql})
        self._record(system, messages, out, cacheable=True)
        return out

    def _record(self, system, messages, out, cacheable):
        sys_t = costs.estimate_tokens(system)
        rest = costs.estimate_tokens("".join(m["content"] for m in messages))
        u = costs.Usage(self.model, "demo", output=costs.estimate_tokens(out), estimated=True, price_as=self.price_as)
        if cacheable and self.caching and sys_t >= 1024:
            t = time.time()
            warm = t - self.cache_clock.get("t", 0) < 300       # the cache lives 5 minutes
            self.cache_clock["t"] = t
            u.input = rest
            if warm:
                u.cache_read = sys_t
            else:
                u.cache_write = sys_t
        else:
            u.input = sys_t + rest
        self.calls.append(u)


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


def with_usage(answer, calls):
    """Attach the model calls (costs.Usage) behind an answer, so the app can show what it cost."""
    answer.usage = list(calls)
    return answer


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
    start = len(getattr(llm, "calls", []))

    def used():
        return list(getattr(llm, "calls", [])[start:])

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
            return with_usage(Answer(obj["clarify"], source="ai", understood=["The model asked a question back."],
                                     ok=False), used())
        sql = obj["sql"]
        try:
            df, truncated = guard.run(conn, sql)
            break
        except Exception as e:
            steps.append(f"Attempt {attempt + 1} failed: {e}")
            messages += [{"role": "assistant", "content": json.dumps({"sql": sql})},
                         {"role": "user", "content": f"That query failed with: {e}. Fix it and reply with JSON only."}]
    else:
        return with_usage(Answer("I couldn't get a working query for that. " + (steps[-1] if steps else ""),
                                 sql=sql or "", source="ai", understood=steps, ok=False), used())

    csv = df.head(40).to_csv(index=False)
    note = f"\n(Showing 40 of {len(df)} rows.)" if len(df) > 40 else ""
    if truncated:
        note += f"\n(The result was cut at {len(df)} rows.)"
    text = llm.complete(ANSWER.format(now=now.strftime("%Y-%m-%d %H:%M")),
                        [{"role": "user", "content": f"Question: {question}\n\nQuery result (CSV):\n{csv}{note}"}])
    return with_usage(Answer(text.strip() or "(The model returned no text.)", sql=guard.clean(sql), data=df,
                             source="ai", understood=steps + [f"Model: {llm.model}"]), used())


def typical_question(price_as, caching=True):
    """Tokens of a typical AI-answered question (two calls), for the cost preview before asking."""
    sys_t = costs.estimate_tokens(SYSTEM.format(schema=schema_text(), kpis=kpi_text(), now="x" * 30, today="x" * 10))
    first = costs.Usage(price_as, "estimate", input=25, output=90, estimated=True)
    if caching:
        first.cache_read = sys_t
    else:
        first.input += sys_t
    second = costs.Usage(price_as, "estimate", input=costs.estimate_tokens(ANSWER) + 120, output=45, estimated=True)
    return [first, second]
