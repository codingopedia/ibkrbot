from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from typing import Callable, Dict, List, Optional

from trader.broker.base import Broker
from trader.events import Fill, MarketEvent, OrderAck, OrderIntent, OrderStatusUpdate
from trader.models import Position


class SimBroker(Broker):
    """A tiny in-memory broker for wiring, tests and dry runs.

    - Market data is generated as a slow random walk.
    - Orders are filled immediately at mid price (simplification).
    """

    def __init__(self) -> None:
        self._log = logging.getLogger("broker.sim")
        self._connected = False
        self._last = 2000.0
        self._fills: List[Fill] = []
        self._seen_fill_ids: set[str] = set()
        self._broker_order_map: Dict[str, str] = {}
        self._seen_status_keys: set[str] = set()
        self._positions: Dict[str, Position] = {}

    def connect(self) -> None:
        self._connected = True
        self._log.info("connected")

    def disconnect(self) -> None:
        self._connected = False
        self._log.info("disconnected")

    def place_order(self, intent: OrderIntent) -> OrderAck:
        if not self._connected:
            raise RuntimeError("SimBroker not connected")
        broker_id = self._broker_order_map.get(intent.client_order_id) or str(uuid.uuid4())
        self._broker_order_map[intent.client_order_id] = broker_id

        fill_price = self._last  # simplistic
        fill = Fill(
            ts=datetime.utcnow(),
            client_order_id=intent.client_order_id,
            broker_order_id=broker_id,
            exec_id=str(uuid.uuid4()),
            symbol=intent.symbol,
            side=intent.side,
            qty=intent.qty,
            price=fill_price,
            commission=0.25,  # placeholder
        )
        self._fills.append(fill)
        self._update_positions(fill)

        return OrderAck(ts=datetime.utcnow(), client_order_id=intent.client_order_id, broker_order_id=broker_id, status="Submitted")

    def subscribe_market_data(self, symbol: str, on_event: Callable[[MarketEvent], None]) -> None:
        if not self._connected:
            raise RuntimeError("SimBroker not connected")

        # Generate a few ticks synchronously (MVP).
        import random

        for _ in range(5):
            self._last += random.uniform(-0.5, 0.5)
            bid = self._last - 0.1
            ask = self._last + 0.1
            on_event(MarketEvent(ts=datetime.utcnow(), symbol=symbol, bid=bid, ask=ask, last=self._last))
            time.sleep(0.1)

    def poll_fills(self, on_fill: Callable[[Fill], None]) -> None:
        # Emit new fills once.
        for f in list(self._fills):
            key = f.broker_order_id + ":" + f.client_order_id
            if key in self._seen_fill_ids:
                continue
            self._seen_fill_ids.add(key)
            on_fill(f)

    def poll_order_status(self, on_status: Callable[[OrderStatusUpdate], None]) -> None:
        for f in list(self._fills):
            key = f.broker_order_id + ":" + f.client_order_id + ":Filled"
            if key in self._seen_status_keys:
                continue
            self._seen_status_keys.add(key)
            on_status(
                OrderStatusUpdate(
                    ts=datetime.utcnow(),
                    client_order_id=f.client_order_id,
                    broker_order_id=f.broker_order_id,
                    status="Filled",
                    filled=f.qty,
                    remaining=0,
                )
            )

    def poll_commissions(self, on_commission: Callable[[str, float], None]) -> None:
        return

    def cancel_order(self, broker_order_id: str) -> None:
        # Sim broker fills immediately; nothing to cancel.
        self._log.info("cancel_order_noop", extra={"broker_order_id": broker_order_id})

    def cancel_all_orders(self, symbol: Optional[str] = None) -> None:
        self._log.info("cancel_all_orders_noop", extra={"symbol": symbol})

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def flatten(self, symbol: str) -> None:
        pos = self._positions.get(symbol)
        qty = pos.qty if pos else 0
        if qty == 0:
            self._log.info("flatten_already_flat", extra={"symbol": symbol})
            return
        side = "SELL" if qty > 0 else "BUY"
        intent = OrderIntent(
            ts=datetime.utcnow(),
            symbol=symbol,
            side=side,
            qty=abs(qty),
            order_type="MKT",
            client_order_id=str(uuid.uuid4()),
        )
        self.place_order(intent)

    def _update_positions(self, fill: Fill) -> None:
        pos = self._positions.get(fill.symbol) or Position(symbol=fill.symbol, qty=0, avg_price=0.0)
        qty_delta = fill.qty if fill.side == "BUY" else -fill.qty
        new_qty = pos.qty + qty_delta
        if new_qty == 0:
            pos.avg_price = 0.0
        else:
            pos.avg_price = fill.price
        pos.qty = new_qty
        self._positions[fill.symbol] = pos
