"""Write the simulated bank into SQLite and keep adding today's payments as time passes."""

import os
import sqlite3
import tempfile
import threading

from . import guard
from .simulate import Settings, lisbon_now, simulate

SCHEMA = """
CREATE TABLE branches (branch_id INTEGER PRIMARY KEY, name TEXT, city TEXT, region TEXT);
CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, segment TEXT, age_band TEXT,
    branch_id INTEGER REFERENCES branches(branch_id), joined_date TEXT, closed_date TEXT, app_user_since TEXT);
CREATE TABLE accounts (account_id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(customer_id),
    product TEXT, opened_date TEXT, closed_date TEXT, balance REAL);
CREATE TABLE transactions (txn_id INTEGER PRIMARY KEY, ts TEXT, customer_id INTEGER REFERENCES customers(customer_id),
    account_id INTEGER REFERENCES accounts(account_id), amount REAL, direction TEXT, type TEXT, channel TEXT,
    merchant_category TEXT);
CREATE TABLE loans (loan_id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(customer_id), product TEXT,
    issued_date TEXT, principal REAL, interest_rate REAL, term_months INTEGER, monthly_payment REAL,
    outstanding REAL, days_past_due INTEGER, status TEXT);
CREATE TABLE balance_snapshots (snapshot_date TEXT, account_id INTEGER REFERENCES accounts(account_id), balance REAL);
CREATE TABLE loan_snapshots (snapshot_date TEXT, loan_id INTEGER REFERENCES loans(loan_id), outstanding REAL,
    days_past_due INTEGER);
CREATE INDEX tx_ts ON transactions(ts);
CREATE INDEX tx_type_ts ON transactions(type, ts);
CREATE INDEX tx_customer ON transactions(customer_id);
CREATE INDEX bal_date ON balance_snapshots(snapshot_date);
CREATE INDEX loan_snap_date ON loan_snapshots(snapshot_date);
CREATE INDEX cust_branch ON customers(branch_id);
CREATE INDEX acc_customer ON accounts(customer_id);
CREATE INDEX loan_customer ON loans(customer_id);
"""

# Plain descriptions: shown on the Data page and given to the AI model.
TABLES = {
    "branches": ("The bank's 8 branches.", {
        "name": "Branch name, e.g. 'Porto Baixa', 'Lisboa Chiado', 'Faro'",
        "city": "Porto, Braga, Aveiro, Coimbra, Lisboa or Faro",
        "region": "Norte, Centro, Lisboa or Algarve"}),
    "customers": ("One row per customer.", {
        "segment": "'Retail', 'Premium' or 'Business'",
        "age_band": "'18-24', '25-34', '35-44', '45-54', '55-64' or '65+'",
        "branch_id": "Home branch",
        "joined_date": "Date the customer joined (YYYY-MM-DD)",
        "closed_date": "Date the customer left; NULL if still a customer",
        "app_user_since": "Date the customer started using the mobile app; NULL if not an app user"}),
    "accounts": ("Deposit accounts. Every customer has one 'Current account'.", {
        "product": "'Current account', 'Savings account' or 'Term deposit'",
        "opened_date": "YYYY-MM-DD", "closed_date": "NULL if open",
        "balance": "Balance today in euros"}),
    "transactions": ("Every payment in and out of current accounts, up to now (live).", {
        "ts": "Local Lisbon time, text 'YYYY-MM-DD HH:MM:SS'",
        "amount": "Euros, always positive; use direction for the sign",
        "direction": "'in' (money received) or 'out' (money paid)",
        "type": "'Salary', 'Pension', 'Transfer in', 'Cash deposit', 'Card payment', 'ATM withdrawal', "
                "'Transfer out', 'Direct debit', 'Loan repayment' or 'Fee'",
        "channel": "'Card', 'ATM', 'App', 'Web', 'Branch' or 'Automatic'",
        "merchant_category": "For card payments: 'Groceries', 'Restaurants', 'Online shopping', 'Fuel', "
                             "'Clothing', 'Entertainment', 'Home & DIY', 'Health' or 'Travel'; else NULL"}),
    "loans": ("Every loan, with its state today.", {
        "product": "'Mortgage', 'Personal loan', 'Car loan' or 'Business loan'",
        "issued_date": "YYYY-MM-DD", "principal": "Amount lent, euros",
        "interest_rate": "Yearly rate in percent, e.g. 3.85",
        "outstanding": "Amount still owed today, euros (0 if repaid)",
        "days_past_due": "Days the payment is late today (0 = on time)",
        "status": "'Current', 'Late' (1-90 days), 'Default' (over 90 days) or 'Repaid'"}),
    "balance_snapshots": ("Balance of every open account at the end of each month; the current month uses today's date.", {
        "snapshot_date": "YYYY-MM-DD (month end, or today for this month)"}),
    "loan_snapshots": ("Outstanding amount and days late of every live loan at the end of each month (today for this month).", {
        "snapshot_date": "YYYY-MM-DD (month end, or today for this month)"}),
}


class Bank:
    """The live database. `catch_up()` adds the payments that have 'happened' since the last call."""

    def __init__(self, now=None, settings=Settings(), path=None):
        self.now = now or lisbon_now()
        self.today = self.now.date()
        data = simulate(self.now, settings)
        self.events = data["events"]
        self.path = path or os.path.join(tempfile.mkdtemp(prefix="bank_"), "bank.sqlite")
        for leftover in (self.path, self.path + "-wal", self.path + "-shm"):
            if os.path.exists(leftover):
                os.remove(leftover)
        self._pending = data["pending"]
        self._lock = threading.Lock()
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        for name in ("branches", "customers", "accounts", "loans", "balance_snapshots", "loan_snapshots"):
            data[name].to_sql(name, conn, if_exists="append", index=False)
        data["transactions"].to_sql("transactions", conn, if_exists="append", index=False, chunksize=50000)
        conn.commit()
        conn.execute("ANALYZE")
        conn.close()

    def catch_up(self, now=None):
        """Insert today's payments up to `now`. Returns how many were added."""
        now = now or lisbon_now()
        with self._lock:
            if self._pending.empty or now <= self.now:
                self.now = max(self.now, now)
                return 0
            cut = now.strftime("%Y-%m-%d %H:%M:%S")
            due = self._pending[self._pending["ts"] <= cut]
            self._pending = self._pending[self._pending["ts"] > cut]
            if len(due):
                conn = sqlite3.connect(self.path, timeout=10)
                due.to_sql("transactions", conn, if_exists="append", index=False)
                conn.commit()
                conn.close()
            self.now = now
            return len(due)

    def connect(self):
        return guard.connect(self.path)
