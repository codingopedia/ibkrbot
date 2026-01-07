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
- Market data permissions missing → doctor/smoke tick wait timeouts.
- `client_id` conflict (another session connected) → connection refused or disconnects.
- Mispriced LMT prevents fills (expected if using far price for reconcile test).
