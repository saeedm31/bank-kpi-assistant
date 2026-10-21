"""Run SQL safely: read-only, SELECT only, known tables only, with a row cap and a time limit.

Several layers, so one mistake does not open the database:
1. The text must be a single SELECT (or WITH ... SELECT) statement.
2. The connection is opened read-only and with `PRAGMA query_only`.
3. An SQLite authorizer allows reading the bank tables and nothing else
   (no writes, no PRAGMA, no ATTACH, no loading extensions).
4. A progress handler stops queries that run too long; results are capped.
"""

import re
import sqlite3
import time

import pandas as pd

TABLES = {"branches", "customers", "accounts", "transactions", "loans", "balance_snapshots", "loan_snapshots"}
BLOCKED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer", "zipfile"}
MAX_ROWS = 2000
TIME_LIMIT = 8.0


class UnsafeQuery(ValueError):
    """The SQL was refused before or while running."""


def clean(sql):
    """Strip code fences, comments at the ends and a trailing semicolon; refuse anything but one SELECT."""
    if not sql or not sql.strip():
        raise UnsafeQuery("Empty query.")
    s = sql.strip()
    s = re.sub(r"^```(?:sql)?\s*|\s*```$", "", s, flags=re.I).strip()
    s = s.rstrip().rstrip(";").strip()
    if ";" in _without_strings(s):
        raise UnsafeQuery("Only one statement is allowed.")
    head = re.sub(r"^(\s*(--[^\n]*\n|/\*.*?\*/))*", "", s, flags=re.S).lstrip().lower()
    if not re.match(r"(select|with)\b", head):
        raise UnsafeQuery("Only SELECT queries are allowed.")
    return s


def _without_strings(s):
    return re.sub(r"'(?:[^']|'')*'", "''", s)


def _authorizer(action, arg1, arg2, dbname, source):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        # arg1 is the table (or a CTE name); internal and pragma tables stay closed
        name = (arg1 or "").lower()
        return sqlite3.SQLITE_DENY if name.startswith(("sqlite_", "pragma_")) else sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_DENY if (arg2 or "").lower() in BLOCKED_FUNCTIONS else sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def connect(path):
    """A read-only connection with the authorizer installed."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False, timeout=10)
    conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(_authorizer)
    return conn


def run(conn, sql, max_rows=MAX_ROWS, time_limit=TIME_LIMIT):
    """Return (DataFrame, truncated). Raises UnsafeQuery or sqlite3.Error with a readable message."""
    sql = clean(sql)
    deadline = time.monotonic() + time_limit
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 20000)
    try:
        cur = conn.execute(sql)
        rows = cur.fetchmany(max_rows + 1)
        cols = [d[0] for d in cur.description or []]
    except sqlite3.DatabaseError as e:
        msg = str(e)
        if "interrupted" in msg:
            raise UnsafeQuery(f"The query took longer than {time_limit:.0f} seconds and was stopped.") from e
        if "not authorized" in msg or "prohibited" in msg or "readonly" in msg.replace("-", ""):
            raise UnsafeQuery("The query tried to do something other than read the bank tables.") from e
        raise
    finally:
        conn.set_progress_handler(None, 0)
    truncated = len(rows) > max_rows
    return pd.DataFrame(rows[:max_rows], columns=cols), truncated
