from __future__ import annotations

import logging
from typing import Iterable, Set

from trader.broker.ibkr.adapter import IBKRBroker
from trader.ledger import Ledger
from trader.persistence import Database
from trader.risk import RiskEngine


def reconcile_ibkr(broker: IBKRBroker, db: Database, ledger: Ledger, risk: RiskEngine, symbol: str) -> None:
    """Reconcile local DB/ledger state with IBKR on startup."""
    log = logging.getLogger("reconcile.ibkr")
    ib = broker.raw_ib()

    # Orders
    ib_open_trades = list(ib.openTrades())
    ib_open_ids: Set[str] = set(str(t.order.orderId) for t in ib_open_trades if t.order)

    db_open = db.get_open_orders()
    db_open_ids: Set[str] = set(o.broker_order_id for o in db_open if o.broker_order_id)

    # IBKR orders missing locally
    for ib_id in ib_open_ids:
        if ib_id not in db_open_ids:
            log.error("ib_order_missing_locally", extra={"broker_order_id": ib_id})
            risk.halt("IBKR open order not in local DB")

    # Local orders missing on IBKR
    for order in db_open:
        if order.broker_order_id and order.broker_order_id not in ib_open_ids:
            log.error(
                "order_missing_on_ibkr",
                extra={"client_order_id": order.client_order_id, "broker_order_id": order.broker_order_id},
            )
            db.update_order_status(order.client_order_id, "MissingOnBroker")

    # Positions
    ib_positions = ib.positions()
    ib_pos_qty = 0
    for p in ib_positions:
        try:
            if getattr(p.contract, "symbol", None) == symbol:
                ib_pos_qty += int(p.position)
        except Exception:
            continue

    ledger_pos_qty = 0
    pos = ledger.positions.get(symbol)
    if pos:
        ledger_pos_qty = pos.qty

    if ib_pos_qty != ledger_pos_qty:
        log.error("position_mismatch", extra={"ib_qty": ib_pos_qty, "ledger_qty": ledger_pos_qty})
        risk.halt("Position mismatch on startup")
