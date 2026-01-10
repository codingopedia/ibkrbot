from __future__ import annotations

import logging
from typing import Set

from trader.broker.ibkr.adapter import IBKRBroker
from trader.ledger import Ledger
from trader.persistence import Database
from trader.risk import RiskEngine


def reconcile_ibkr(
    broker: IBKRBroker,
    db: Database,
    ledger: Ledger,
    risk: RiskEngine,
    symbol: str,
    *,
    instance_id: str,
    unknown_orders_policy: str,
    env: str,
) -> None:
    """Reconcile local DB/ledger state with IBKR on startup."""
    log = logging.getLogger("reconcile.ibkr")
    ib = broker.raw_ib()

    prefix = f"BOT:{instance_id}:"

    def _is_ours(order_ref: str) -> bool:
        return order_ref.startswith(prefix)

    # Orders
    ib_open_trades = list(ib.openTrades())
    ib_open_ids: Set[str] = set()
    unknown_ib_order_ids: list[str] = []

    for t in ib_open_trades:
        if not getattr(t, "order", None):
            continue
        order_id = str(t.order.orderId)
        order_ref = getattr(t.order, "orderRef", "") or ""
        if _is_ours(order_ref):
            ib_open_ids.add(order_id)
        else:
            unknown_ib_order_ids.append(order_id)

    policy = unknown_orders_policy.upper()
    if unknown_ib_order_ids:
        if policy == "HALT":
            log.error("unknown_ib_orders_halt", extra={"order_ids": unknown_ib_order_ids})
            risk.halt("Unknown IBKR open orders present")
        elif policy == "IGNORE":
            log.warning("unknown_ib_orders_ignore", extra={"order_ids": unknown_ib_order_ids})
        elif policy == "CANCEL":
            if env != "paper":
                log.error("unknown_ib_orders_cancel_denied", extra={"order_ids": unknown_ib_order_ids, "env": env})
                risk.halt("Refusing to cancel unknown IBKR orders outside paper env")
            else:
                for oid in unknown_ib_order_ids:
                    try:
                        broker.cancel_order(oid)
                    except Exception:
                        log.exception("unknown_ib_order_cancel_failed", extra={"order_id": oid})
                log.warning("unknown_ib_orders_cancelled", extra={"order_ids": unknown_ib_order_ids})
        else:
            log.error("unknown_orders_policy_invalid", extra={"policy": policy})
            risk.halt("Invalid unknown_orders_policy")

    db_open = db.get_open_orders()
    db_open_ids: Set[str] = set(o.broker_order_id for o in db_open if o.broker_order_id)

    # IBKR orders missing locally (ours only)
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
