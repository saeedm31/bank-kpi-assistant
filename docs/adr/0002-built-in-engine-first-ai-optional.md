# 2. A built-in engine first, AI mode optional

**Status:** accepted

## Context
Most questions managers ask are about a known KPI, a period and a filter. Sending each one to an LLM costs money,
adds delay and can vary.

## Decision
A rule-based engine answers KPI questions for free and instantly, and says plainly when it cannot place a word.
AI mode, with the user's own key, handles everything else and shows tokens and cost under each answer.

## Consequences
- The demo works with no key, and its accuracy can be tested exactly.
- The engine is predictable by design, so it declines more often than a model would.
- AI answers must be checked with the SQL shown; the app makes that easy.
