# 1. One definition per KPI, in a file both engines use

**Status:** accepted

## Context
"Chat with your database" tools answer fast, but two people asking the same question can get different numbers,
because the model writes new SQL each time.

## Decision
Every metric is defined once in `kpis.yml` (source, SQL expression, always-on filter, unit), with dimensions and
synonyms. The built-in engine builds SQL only from these definitions, and AI mode gets them in its instructions.

## Consequences
- The same question gives the same number, however it is worded.
- Adding a KPI is a reviewed change to one file, which the finance or data team can own.
- Questions outside the list need AI mode or a saved example.
