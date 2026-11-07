# Evaluation

`python evaluate.py` asks the built-in engine every question in a test set and compares its numbers with
hand-written SQL. `python evaluate.py --ai openai` (or `--ai anthropic`) scores AI mode on the same questions.

## Test sets

| Set | File | What it checks |
|---|---|---|
| Main | `eval/questions.yml` | 40 KPI questions and 8 out-of-scope questions that must be declined |
| Unseen | `eval/holdout.yml` | questions written after the engine was tuned on the main set, not used to tune it |
| New wording, first try | `eval/holdout2.yml` | 20 questions written in advance and scored once, before any fix ([result](../reports/eval_unseen_first_run.json)) |

## What counts as right

An answer is right only when its numbers match the hand-written SQL. Declining ("I can't answer that") is right
for out-of-scope questions (forecasts, "why", changing data, unknown topics) and is counted separately from wrong
answers on the others: a decline is safe, a wrong number is not.

## Alerts

The simulator plants events (a card terminal outage, a burst of large transfers, a rush on ATMs). The tests check
that each is found and count false alerts over all simulated days.

Results are in the [README](../README.md#results).
