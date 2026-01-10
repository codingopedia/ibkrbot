# Runbook: Paper IBKR Checks

## Pre-checks (no orders)
- Verify network port open: `Test-NetConnection -ComputerName 127.0.0.1 -Port 4002` (paper GW), optionally 4001/7497/7496 depending on GW/TWS.
- Confirm IB Gateway/TWS API is running with matching `clientId` and read-only mode as needed.
- Ensure config points to paper env and correct host/port/client_id.

## Sequence
1) **doctor** (read-only): `python -m trader doctor -c config\paper.ibkr.example.yaml`
   - Expect serverVersion/managedAccounts/currentTime, nearest contracts, and at least one tick.
2) **smoke MKT**: `python -m trader smoke -c config\paper.ibkr.example.yaml --qty 1 --side BUY --order-type MKT`
   - Wait for fill; check orders/fills/pnl snapshots and commission backfill logs.
3) **smoke LMT no-wait** (leave open order): `python -m trader smoke -c config\paper.ibkr.example.yaml --order-type LMT --limit-price <far_price> --no-wait`
   - Confirms order creation/DB persistence without waiting for fill.
4) **run** (reconcile should detect open LMT): `python -m trader run -c config\paper.ibkr.example.yaml --iterations 0`
   - Reconcile should halt if unexpected open orders/position mismatch; review logs.
5) **flatten** (panic): `python -m trader flatten -c config\paper.ibkr.example.yaml --confirm`
   - Cancels all orders and flattens position via MKT.

## What to check
- `run\trader.log`: reconcile results, commission_backfilled, order status updates, fills, pnl snapshots.
- SQLite `run\trader.sqlite`: tables `orders` (status updates, exec_id), `fills` (exec_id/commission), `pnl_snapshots`.

## Typical issues
- Ports blocked/incorrect (paper GW: 4002, live GW: 4001, TWS: 7497/7496).
- Market data permissions missing -> doctor/smoke tick wait timeouts.
- `client_id` conflict (another session connected) -> connection refused or disconnects.
- Mispriced LMT prevents fills (expected if using far price for reconcile test).

## Trading enablement
- `run` defaults to read-only: `trading.enabled=false` blocks order placement but still streams data/reconciles/publishes PnL.
- To enable paper trading set in config:
  ```yaml
  trading:
    enabled: true
    allow_live: false
  ```
- Live trading additionally requires `env: live` **and** `trading.allow_live: true`; otherwise startup is refused.
- When `allow_live=true`, logs emit **LIVE TRADING ENABLED**; double-check host/port/account before proceeding.
- Smoke/flatten stay operational tools and bypass the `trading.enabled` gate (smoke remains paper-only; flatten needs `--confirm`).
- Recommended: keep a paper config committed and a separate live override with `allow_live=true` only when needed.

## 2-week trial workflow
- Run read-only for data/logic validation: `python -m trader run -c config\paper.tws.orb_a.local.yaml --iterations <N>` (bars+signals saved; trading.enabled stays false).
- Optional after a few days: flip `trading.enabled=true` (paper) in a copy of the config; keep `allow_live=false`.
- Daily routine: (1) run collector (read-only or paper), (2) export CSVs `python -m trader export -c <cfg> --outdir run/exports --days 14`, (3) generate summary `python -m trader report -c <cfg> --days 14` (JSON lands in `run/reports/`).
- One-click dry trial: `python -m trader trial -c <cfg> --iterations 3600 --outdir run/trials --export-days 14` (refuses to run if `trading.enabled=true`; saves reports to `<outdir>/reports` and CSVs to `<outdir>/exports`).
