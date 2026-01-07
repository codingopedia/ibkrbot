from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from trader.config import RiskConfig
from trader.events import OrderIntent
from trader.models import PnLSnapshot, Position


class RiskRejected(Exception):
    pass


class RiskEngine:
    def __init__(self, cfg: RiskConfig) -> None:
        self.cfg = cfg
        self._log = logging.getLogger("risk")
        self._halted = False

    @property
    def halted(self) -> bool:
        return self._halted

    def evaluate_order(self, intent: OrderIntent, current_pos_qty: int) -> None:
        if self._halted:
            raise RiskRejected("Trading halted")

        if abs(intent.qty) > self.cfg.max_order_size:
            raise RiskRejected(f"Order too large: {intent.qty} > {self.cfg.max_order_size}")

        # Position limit
        new_pos = current_pos_qty + (intent.qty if intent.side == "BUY" else -intent.qty)
        if abs(new_pos) > self.cfg.max_position:
            raise RiskRejected(f"Position limit breach: {new_pos} > {self.cfg.max_position}")

    def evaluate_pnl(self, snap: PnLSnapshot) -> None:
        # Very simple daily loss guard (realized + unrealized - commissions).
        total = snap.realized_usd + snap.unrealized_usd - snap.commissions_usd
        if total <= -abs(self.cfg.max_daily_loss_usd):
            self._halted = True
            self._log.error("max_daily_loss_hit", extra={"symbol": snap.symbol, "pnl_total_usd": total})

    def halt(self, reason: str) -> None:
        self._halted = True
        self._log.error("trading_halt", extra={"reason": reason})
