# Data card: the simulated bank

| | |
|---|---|
| **Source** | `bank_assistant/simulate.py` and `db.py`, built on first start, relative to "now" in Lisbon |
| **Bank** | A fictional Portuguese retail bank |
| **Size** | 8 branches (Porto, Braga, Aveiro, Coimbra, Lisboa, Faro), 2,000 customers, about 700,000 payments over two years |
| **Live** | New payments are added every minute, so "today" and "this week so far" are real |
| **Personal data** | None. All customers are invented |

## Tables

| Table | One row per |
|---|---|
| `branches` | branch: city, region |
| `customers` | customer: segment (Retail, Premium, Business), age band, branch, joined and closed dates, app user since |
| `accounts` | current account, savings or term deposit |
| `transactions` | payment: time, amount, direction, type (card payment, ATM withdrawal, transfer...), channel, merchant category |
| `loans` | mortgage, personal, car or business loan: principal, rate, term, amount owed, days late |
| `balance_snapshots` | account balance at each month end (and today) |
| `loan_snapshots` | amount owed and days late at each month end (and today) |

## Planted patterns

Christmas and summer peaks · the move from cash to cards and from branches to the app · 14 salaries a year (June
and November) · interest rates up in 2023 and down from 2024 · customers whose salary stops before they leave ·
late payments rising on personal loans in the Algarve · a few events for the alerts to find (a card terminal
outage, a burst of large transfers, a rush on ATMs).

## Limits
The patterns are planted, so the "insights" are there by design. Snapshots are monthly, so balances and the loan
book are only known at month ends and today.
