# 3. Every query passes a layered guard

**Status:** accepted

## Context
SQL can come from three places: the built-in engine, a saved example or a model. Any of them could be wrong, and
a model could be talked into writing something harmful.

## Decision
All SQL runs through `guard.py`: one SELECT only, a read-only connection with `PRAGMA query_only`, an SQLite
authorizer that allows only the 7 bank tables and blocks file and extension functions, an 8-second time limit and a
2,000-row limit.

## Consequences
- One missed check does not open the database; the next layer still holds.
- The same rules apply whatever wrote the SQL.
- In production, the read-only warehouse role is one more layer, not a replacement.
