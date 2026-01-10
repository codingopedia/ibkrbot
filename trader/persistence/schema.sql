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

CREATE TABLE IF NOT EXISTS bars_1m (
  ts TEXT NOT NULL,
  symbol TEXT NOT NULL,
  open REAL NOT NULL,
  high REAL NOT NULL,
  low REAL NOT NULL,
  close REAL NOT NULL,
  volume REAL NOT NULL,
  UNIQUE(symbol, ts)
);

CREATE TABLE IF NOT EXISTS strategy_signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  symbol TEXT NOT NULL,
  strategy TEXT NOT NULL,
  type TEXT NOT NULL,
  side TEXT,
  qty INTEGER,
  reason TEXT,
  price_ref REAL,
  bar_ts TEXT,
  is_snapshot INTEGER NOT NULL DEFAULT 0,
  extras_json TEXT
);

CREATE TABLE IF NOT EXISTS trade_journal (
  trade_id TEXT PRIMARY KEY,
  instance_id TEXT,
  strategy TEXT,
  symbol TEXT NOT NULL,
  entry_ts TEXT,
  entry_price REAL,
  entry_side TEXT,
  entry_reason TEXT,
  exit_ts TEXT,
  exit_price REAL,
  exit_reason TEXT,
  qty INTEGER,
  pnl_usd REAL,
  range_high REAL,
  range_low REAL,
  risk_per_unit REAL
);

CREATE TABLE IF NOT EXISTS trade_metrics (
  trade_id TEXT PRIMARY KEY,
  duration_seconds REAL,
  mfe REAL,
  mae REAL,
  r_multiple REAL
);

CREATE TABLE IF NOT EXISTS trade_shadow (
  trade_id TEXT NOT NULL,
  variant_name TEXT NOT NULL,
  exit_ts TEXT,
  exit_price REAL,
  pnl_usd REAL,
  reason_exit TEXT,
  PRIMARY KEY (trade_id, variant_name)
);

CREATE TABLE IF NOT EXISTS strategy_daily (
  day TEXT NOT NULL,
  symbol TEXT NOT NULL,
  strategy TEXT NOT NULL,
  timezone TEXT NOT NULL,
  range_start TEXT,
  range_end TEXT,
  entry_start TEXT,
  entry_end TEXT,
  range_high REAL,
  range_low REAL,
  range_bars INTEGER NOT NULL,
  signals_count INTEGER NOT NULL,
  entries_count INTEGER NOT NULL,
  exits_count INTEGER NOT NULL,
  trades_closed_count INTEGER NOT NULL,
  notes_json TEXT,
  PRIMARY KEY(day, symbol, strategy)
);
