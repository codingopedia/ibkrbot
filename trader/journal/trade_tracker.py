from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

from trader.events import Fill
from trader.journal.analytics import compute_mae_mfe, compute_shadow_exits
from trader.persistence.db import Database


class TradeTracker:
    """Track trades based on fills and persist journaling artifacts."""

    def __init__(
        self,
        db: Database,
        *,
        instance_id: str,
        strategy_name: str,
        symbol: str,
        price_multiplier: float,
        tz_name: str,
        shadow_cfg: Any,
        logger: Optional[logging.Logger] = None,
        on_trade_closed: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.db = db
        self.instance_id = instance_id
        self.strategy_name = strategy_name
        self.symbol = symbol
        self.price_multiplier = float(price_multiplier)
        self.tz_name = tz_name
        self.shadow_cfg = shadow_cfg
        self._log = logger or logging.getLogger("trade_tracker")
        self._on_trade_closed = on_trade_closed

        self._pending_context: Dict[str, Any] = {}
        self._active_trade: Optional[Dict[str, Any]] = None
        self._last_exit_reason: Optional[str] = None

    def set_pending_context(self, ctx: Dict[str, Any]) -> None:
        self._pending_context = dict(ctx or {})

    def set_exit_reason(self, reason: str) -> None:
        self._last_exit_reason = reason

    def on_fill(self, fill: Fill, position_before: int, position_after: int) -> None:
        # open
        if position_before == 0 and position_after != 0:
            self._start_trade(fill, position_after)
            return
        # close
        if position_before != 0 and position_after == 0:
            self._close_trade(fill, abs(position_before))
            return
        # flip: close old then open new
        if position_before != 0 and position_after != 0 and (position_before > 0) != (position_after > 0):
            self._close_trade(fill, abs(position_before))
            self._start_trade(fill, position_after)

    def _start_trade(self, fill: Fill, position_qty_after: int) -> None:
        trade_id = self._pending_context.get("trade_id") or f"trade-{uuid.uuid4().hex[:10]}"
        range_high = self._pending_context.get("range_high")
        range_low = self._pending_context.get("range_low")
        risk_per_unit = self._pending_context.get("risk_per_unit")
        stop_price = self._pending_context.get("stop_price")
        tp_price = self._pending_context.get("tp_price")
        entry_reason = self._pending_context.get("entry_reason", "")

        self._active_trade = {
            "trade_id": trade_id,
            "entry_ts": fill.ts,
            "entry_price": fill.price,
            "entry_side": fill.side,
            "qty": abs(position_qty_after),
            "range_high": range_high,
            "range_low": range_low,
            "risk_per_unit": risk_per_unit,
            "stop_price": stop_price,
            "tp_price": tp_price,
            "entry_reason": entry_reason,
            "flat_time": self._pending_context.get("flat_time"),
        }
        self.db.upsert_trade(
            trade_id=trade_id,
            instance_id=self.instance_id,
            strategy=self.strategy_name,
            symbol=self.symbol,
            entry_ts=fill.ts,
            entry_price=fill.price,
            entry_side=fill.side,
            entry_reason=entry_reason,
            exit_ts=None,
            exit_price=None,
            exit_reason=None,
            qty=abs(position_qty_after),
            pnl_usd=None,
            range_high=range_high,
            range_low=range_low,
            risk_per_unit=risk_per_unit,
        )
        self._log.info(
            "trade_started",
            extra={
                "trade_id": trade_id,
                "side": fill.side,
                "qty": position_qty_after,
                "entry_price": fill.price,
                "range_high": range_high,
                "range_low": range_low,
            },
        )
        self._pending_context = {}

    def _close_trade(self, fill: Fill, closing_qty: int) -> None:
        if not self._active_trade:
            return
        trade = self._active_trade
        trade_id = trade.get("trade_id") or f"trade-{uuid.uuid4().hex[:8]}"
        entry_price = float(trade.get("entry_price", fill.price))
        side = trade.get("entry_side", fill.side)
        qty = int(trade.get("qty") or closing_qty)
        multiplier = self.price_multiplier

        pnl = (fill.price - entry_price) * qty * multiplier if side == "BUY" else (entry_price - fill.price) * qty * multiplier
        exit_reason = self._last_exit_reason or trade.get("exit_reason") or ""
        self.db.upsert_trade(
            trade_id=trade_id,
            instance_id=self.instance_id,
            strategy=self.strategy_name,
            symbol=self.symbol,
            entry_ts=trade.get("entry_ts"),
            entry_price=entry_price,
            entry_side=side,
            entry_reason=trade.get("entry_reason"),
            exit_ts=fill.ts,
            exit_price=fill.price,
            exit_reason=exit_reason,
            qty=qty,
            pnl_usd=pnl,
            range_high=trade.get("range_high"),
            range_low=trade.get("range_low"),
            risk_per_unit=trade.get("risk_per_unit"),
        )

        # Analytics
        bars = self.db.get_bars_between(self.symbol, trade.get("entry_ts"), fill.ts) if trade.get("entry_ts") else []
        mae, mfe, r_multiple = compute_mae_mfe(
            bars, side, entry_price, fill.price, trade.get("risk_per_unit") or None
        )
        duration_seconds = None
        if trade.get("entry_ts"):
            duration_seconds = (fill.ts - trade["entry_ts"]).total_seconds()
        self.db.upsert_trade_metrics(
            trade_id=trade_id,
            duration_seconds=duration_seconds,
            mfe=mfe,
            mae=mae,
            r_multiple=r_multiple,
        )

        if self.shadow_cfg and getattr(self.shadow_cfg, "enabled", False) and bars:
            try:
                shadow_results = compute_shadow_exits(
                    trade_id=trade_id,
                    bars=bars,
                    side=side,
                    entry_price=entry_price,
                    qty=qty,
                    price_multiplier=multiplier,
                    risk_per_unit=trade.get("risk_per_unit") or 0.0,
                    stop_price=trade.get("stop_price") or entry_price,
                    tp_price=trade.get("tp_price") or entry_price,
                    flat_time=trade.get("flat_time"),
                    tz_name=self.tz_name,
                    variant_b_tp_r=getattr(self.shadow_cfg, "variant_b_tp_r", 0.0),
                    variant_c_sl_tight_r=getattr(self.shadow_cfg, "variant_c_sl_tight_r", 0.0),
                )
                for res in shadow_results:
                    self.db.upsert_trade_shadow(
                        trade_id=res.trade_id,
                        variant_name=res.variant_name,
                        exit_ts=res.exit_ts,
                        exit_price=res.exit_price,
                        pnl_usd=res.pnl_usd,
                        reason_exit=res.reason_exit,
                    )
            except Exception:
                self._log.warning("shadow_computation_failed", extra={"trade_id": trade_id})

        self._log.info(
            "trade_closed",
            extra={
                "trade_id": trade_id,
                "exit_reason": exit_reason,
                "pnl_usd": pnl,
                "duration_seconds": duration_seconds,
            },
        )
        self._active_trade = None
        self._last_exit_reason = None
        if self._on_trade_closed:
            day_local = fill.ts.astimezone(ZoneInfo(self.tz_name)).date().isoformat()
            self._on_trade_closed(day_local)
