from __future__ import annotations

import logging
import random
import time
import uuid
from datetime import datetime
from typing import Callable, Dict, List, Optional

from trader.broker.base import Broker
from trader.events import BarEvent, Fill, MarketEvent, OrderAck, OrderIntent, OrderStatusUpdate
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
        self._bar_cb: Optional[Callable[[BarEvent], None]] = None
        self._bar_symbol: Optional[str] = None
        self._last_bar_ts: Optional[datetime] = None

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
        for _ in range(5):
            self._last += random.uniform(-0.5, 0.5)
            bid = self._last - 0.1
            ask = self._last + 0.1
            on_event(MarketEvent(ts=datetime.utcnow(), symbol=symbol, bid=bid, ask=ask, last=self._last))
            time.sleep(0.1)

    def subscribe_bars(self, symbol: str, on_bar: Callable[[BarEvent], None]) -> None:
        if not self._connected:
            raise RuntimeError("SimBroker not connected")
        if self._bar_cb is not None:
            return
        self._bar_cb = on_bar
        self._bar_symbol = symbol
        self._log.info("bars_subscribed_sim", extra={"symbol": symbol})

    def poll_bars(self) -> None:
        if not self._connected or not self._bar_cb or not self._bar_symbol:
            return
        now = datetime.utcnow()
        # Emit at most one bar per second
        if self._last_bar_ts and (now - self._last_bar_ts).total_seconds() < 1.0:
            return
        self._last += random.uniform(-0.3, 0.3)
        o = self._last + random.uniform(-0.1, 0.1)
        c = self._last
        high = max(o, c) + random.uniform(0.0, 0.2)
        low = min(o, c) - random.uniform(0.0, 0.2)
        vol = abs(random.randint(50, 150))
        bar = BarEvent(ts_utc=now, symbol=self._bar_symbol, open=o, high=high, low=low, close=c, volume=vol)
        self._bar_cb(bar)
        self._last_bar_ts = now

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
