-- Ledger-first tables. Extend as needed.

PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  client_order_id TEXT NOT NULL,
  broker_order_id TEXT NOT NULL,
  exec_id TEXT UNIQUE,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  qty INTEGER NOT NULL,
  price REAL NOT NULL,
  commission REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pnl_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  symbol TEXT NOT NULL,
  position_qty INTEGER NOT NULL,
  avg_price REAL NOT NULL,
  last_price REAL,
  unrealized_usd REAL NOT NULL,
  realized_usd REAL NOT NULL,
  commissions_usd REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
  client_order_id TEXT PRIMARY KEY,
  broker_order_id TEXT,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  qty INTEGER NOT NULL,
  order_type TEXT NOT NULL,
  limit_price REAL,
  status TEXT NOT NULL,
  created_ts TEXT NOT NULL,
  updated_ts TEXT NOT NULL
);
