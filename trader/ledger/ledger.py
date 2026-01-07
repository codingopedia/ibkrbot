from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime
from typing import Dict, Optional

from trader.events import Fill
from trader.models import PnLSnapshot, Position


class Ledger:
    """In-memory position + realized PnL tracker.

    Source of truth in the system is the ledger. We can persist all fills and snapshots to SQLite.
    """

    def __init__(self, price_multiplier: float = 1.0) -> None:
        self._log = logging.getLogger("ledger")
        self.price_multiplier = float(price_multiplier)
        self.positions: Dict[str, Position] = {}
        self.realized_usd: Dict[str, float] = {}
        self.commissions_usd: Dict[str, float] = {}

    def on_fill(self, fill: Fill) -> None:
        pos = self.positions.get(fill.symbol) or Position(symbol=fill.symbol, qty=0, avg_price=0.0)
        realized = self.realized_usd.get(fill.symbol, 0.0)
        comm = self.commissions_usd.get(fill.symbol, 0.0) + float(fill.commission)
        self.commissions_usd[fill.symbol] = comm

        qty = fill.qty if fill.side == "BUY" else -fill.qty

        # If adding to same direction -> update avg
        if pos.qty == 0 or (pos.qty > 0 and qty > 0) or (pos.qty < 0 and qty < 0):
            new_qty = pos.qty + qty
            if new_qty != 0:
                pos.avg_price = (pos.avg_price * pos.qty + fill.price * qty) / new_qty
            pos.qty = new_qty
        else:
            # Reducing / flipping: realize PnL on closed portion
            closing_qty = min(abs(pos.qty), abs(qty))
            sign = 1 if pos.qty > 0 else -1
            # If we were long: sell realizes (sell_price - avg)*qty
            # If we were short: buy realizes (avg - buy_price)*qty
            pnl_per_unit = (fill.price - pos.avg_price) * sign * -1  # sign logic
            # Let's compute cleanly:
            if pos.qty > 0 and qty < 0:
                pnl = (fill.price - pos.avg_price) * closing_qty
            elif pos.qty < 0 and qty > 0:
                pnl = (pos.avg_price - fill.price) * closing_qty
            else:
                pnl = 0.0
            realized += pnl * self.price_multiplier
            self.realized_usd[fill.symbol] = realized

            pos.qty = pos.qty + qty
            if pos.qty == 0:
                pos.avg_price = 0.0
            # If flipped, avg becomes fill price for remaining qty (simplification)
            if (pos.qty > 0 and qty > 0) or (pos.qty < 0 and qty < 0):
                pos.avg_price = fill.price

        self.positions[fill.symbol] = pos
        self._log.info(
            "fill_applied",
            extra={
                "symbol": fill.symbol,
                "side": fill.side,
                "qty": fill.qty,
                "price": fill.price,
                "pos_qty": pos.qty,
                "pos_avg": pos.avg_price,
                "realized_usd": self.realized_usd.get(fill.symbol, 0.0),
                "commissions_usd": self.commissions_usd.get(fill.symbol, 0.0),
            },
        )

    def snapshot(self, symbol: str, last_price: Optional[float]) -> PnLSnapshot:
        pos = self.positions.get(symbol) or Position(symbol=symbol, qty=0, avg_price=0.0)
        realized = float(self.realized_usd.get(symbol, 0.0))
        comm = float(self.commissions_usd.get(symbol, 0.0))
        if last_price is None or pos.qty == 0:
            unreal = 0.0
        else:
            # long: (last-avg)*qty ; short: (avg-last)*abs(qty)
            if pos.qty > 0:
                unreal = (last_price - pos.avg_price) * pos.qty * self.price_multiplier
            else:
                unreal = (pos.avg_price - last_price) * abs(pos.qty) * self.price_multiplier

        return PnLSnapshot(
            ts=datetime.utcnow(),
            symbol=symbol,
            position_qty=pos.qty,
            avg_price=pos.avg_price,
            last_price=last_price,
            unrealized_usd=float(unreal),
            realized_usd=realized,
            commissions_usd=comm,
        )
