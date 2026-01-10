from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from trader.config import StrategyORBVariantAConfig
from trader.events import BarEvent, MarketEvent, Signal
from trader.strategy.base import Strategy


@dataclass
class _TradeState:
    side: str
    entry_price: float
    stop_price: float
    tp_price: float
    risk_per_unit: float
    entry_ts: datetime
    be_armed: bool = False
    be_trigger_price: Optional[float] = None
    be_offset: float = 0.0


class ORBVariantAStrategy(Strategy):
    """Opening Range Breakout (Variant A) intraday strategy."""

    def __init__(self, cfg: StrategyORBVariantAConfig, symbol: str, logger: Optional[logging.Logger] = None) -> None:
        self.cfg = cfg
        self.symbol = symbol
        self._log = logger or logging.getLogger("strategy.orb_variant_a")
        self._tz = ZoneInfo(cfg.timezone)

        self._current_day: Optional[datetime.date] = None
        self._range_high: Optional[float] = None
        self._range_low: Optional[float] = None
        self._trades_today: int = 0
        self._signals_count: int = 0
        self._entries_count: int = 0
        self._exits_count: int = 0
        self._range_bars: int = 0
        self._active: Optional[_TradeState] = None
        self._last_signal_context: Dict[str, Any] = {}
        self._range_logged: bool = False
        self._completed_days: List[Dict[str, Any]] = []

        self._range_window = self._parse_window(cfg.range_window.start, cfg.range_window.end)
        self._entry_window = self._parse_window(cfg.entry_window.start, cfg.entry_window.end)
        self._flat_time = self._parse_time(cfg.flat_time)

    def on_market(self, event: MarketEvent) -> Optional[Signal]:
        return None

    def on_bar(self, bar: BarEvent, position_qty: int) -> Optional[Signal]:
        ts_local = self._to_local(bar.ts_utc)
        self._last_signal_context = {}
        self._roll_day_if_needed(ts_local)
        self._update_range(bar)

        exit_sig = self._maybe_exit(bar, ts_local, position_qty)
        if exit_sig:
            return exit_sig

        if bar.is_snapshot:
            return None

        if not self._range_ready:
            return None
        if not self._in_window(ts_local, self._entry_window):
            return None
        if self._trades_today >= self.cfg.max_trades_per_day:
            return None
        if position_qty != 0 or self._active:
            return None

        buffer = self.cfg.breakout_buffer_ticks * self.cfg.min_tick
        long_trigger = bar.close > (self._range_high + buffer)  # type: ignore[operator]
        short_trigger = self.cfg.allow_short and bar.close < (self._range_low - buffer)  # type: ignore[operator]

        if long_trigger:
            return self._build_entry(bar, side="BUY")
        if short_trigger:
            return self._build_entry(bar, side="SELL")
        return None

    def get_signal_context(self) -> Dict[str, Any]:
        return dict(self._last_signal_context)

    def get_daily_state(self) -> Optional[Dict[str, Any]]:
        if self._current_day is None:
            return None
        return {
            "day": self._current_day.isoformat(),
            "timezone": self.cfg.timezone,
            "symbol": self.symbol,
            "strategy": "orb_variant_a",
            "range_start": self.cfg.range_window.start,
            "range_end": self.cfg.range_window.end,
            "entry_start": self.cfg.entry_window.start,
            "entry_end": self.cfg.entry_window.end,
            "range_high": self._range_high,
            "range_low": self._range_low,
            "range_bars": self._range_bars,
            "signals_count": self._signals_count,
            "entries_count": self._entries_count,
            "exits_count": self._exits_count,
        }

    def consume_completed_days(self) -> List[Dict[str, Any]]:
        completed = list(self._completed_days)
        self._completed_days.clear()
        return completed

    # Internal helpers
    def _parse_time(self, value: str) -> time:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour=hour, minute=minute)

    def _parse_window(self, start: str, end: str) -> tuple[time, time]:
        return (self._parse_time(start), self._parse_time(end))

    def _to_local(self, ts: datetime) -> datetime:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(self._tz)

    def _roll_day_if_needed(self, ts_local: datetime) -> None:
        day = ts_local.date()
        if self._current_day == day:
            return
        if self._current_day is not None:
            self._completed_days.append(self._build_day_summary(self._current_day))
        self._current_day = day
        self._range_high = None
        self._range_low = None
        self._active = None
        self._trades_today = 0
        self._signals_count = 0
        self._entries_count = 0
        self._exits_count = 0
        self._range_bars = 0
        self._range_logged = False
        self._log.info("orb_day_reset", extra={"day": day.isoformat()})

    def _update_range(self, bar: BarEvent) -> None:
        ts_local = self._to_local(bar.ts_utc)
        if not self._in_window(ts_local, self._range_window):
            return
        self._range_high = bar.high if self._range_high is None else max(self._range_high, bar.high)
        self._range_low = bar.low if self._range_low is None else min(self._range_low, bar.low)
        self._last_signal_context = {
            "range_high": self._range_high,
            "range_low": self._range_low,
            "type": "range_update",
            "bar_ts": bar.ts_utc.isoformat(),
            "day": self._current_day.isoformat() if self._current_day else None,
            "is_range_window": True,
            "range_bars": self._range_bars + 1,
        }
        if not self._range_logged and self._range_ready:
            self._range_logged = True
            self._log.info(
                "orb_range_built",
                extra={"range_high": self._range_high, "range_low": self._range_low, "bar_ts": bar.ts_utc.isoformat()},
            )
        self._range_bars += 1

    @property
    def _range_ready(self) -> bool:
        return self._range_high is not None and self._range_low is not None

    def _in_window(self, ts_local: datetime, window: tuple[time, time]) -> bool:
        t = ts_local.timetz()
        start, end = window
        return start <= t.replace(tzinfo=None) <= end

    def _build_entry(self, bar: BarEvent, side: str) -> Signal:
        assert self._range_ready
        ts_local = self._to_local(bar.ts_utc)
        stop_buffer = self.cfg.sl_buffer_ticks * self.cfg.min_tick
        if side == "BUY":
            stop_price = (self._range_low or 0.0) - stop_buffer
        else:
            stop_price = (self._range_high or 0.0) + stop_buffer
        risk = abs(bar.close - stop_price)
        tp_price = bar.close + self.cfg.tp_r * risk if side == "BUY" else bar.close - self.cfg.tp_r * risk

        be_trigger = None
        be_offset = 0.0
        if self.cfg.be.enabled and self.cfg.be.trigger_r > 0:
            move = self.cfg.be.trigger_r * risk
            be_trigger = bar.close + move if side == "BUY" else bar.close - move
            be_offset = self.cfg.be.offset_ticks * self.cfg.min_tick

        self._active = _TradeState(
            side=side,
            entry_price=bar.close,
            stop_price=stop_price,
            tp_price=tp_price,
            risk_per_unit=risk,
            entry_ts=bar.ts_utc,
            be_armed=False,
            be_trigger_price=be_trigger,
            be_offset=be_offset,
        )
        self._trades_today += 1
        self._signals_count += 1
        self._entries_count += 1
        self._last_signal_context = {
            "type": "entry",
            "side": side,
            "range_high": self._range_high,
            "range_low": self._range_low,
            "stop_price": stop_price,
            "tp_price": tp_price,
            "risk_per_unit": risk,
            "be_trigger_price": be_trigger,
            "qty": self.cfg.qty,
            "bar_ts": bar.ts_utc.isoformat(),
            "entry_reason": "orb_entry_long" if side == "BUY" else "orb_entry_short",
            "flat_time": self.cfg.flat_time,
            "day": self._current_day.isoformat() if self._current_day else None,
            "range_bars": self._range_bars,
            "is_range_window": self._in_window(ts_local, self._range_window),
            "is_entry_window": self._in_window(ts_local, self._entry_window),
        }
        reason = "orb_entry_long" if side == "BUY" else "orb_entry_short"
        self._log.info(
            "orb_entry_signal",
            extra={
                "side": side,
                "entry_price": bar.close,
                "stop_price": stop_price,
                "tp_price": tp_price,
                "range_high": self._range_high,
                "range_low": self._range_low,
            },
        )
        return Signal(ts=bar.ts_utc, symbol=self.symbol, side=side, qty=self.cfg.qty, reason=reason)

    def _maybe_exit(self, bar: BarEvent, ts_local: datetime, position_qty: int) -> Optional[Signal]:
        if self._active is None:
            return None

        active = self._active
        side = active.side
        exit_side = "SELL" if side == "BUY" else "BUY"

        self._maybe_move_to_be(bar, active)

        stop_hit = False
        tp_hit = False
        exit_reason = ""
        exit_price = bar.close

        if side == "BUY":
            if bar.low <= active.stop_price:
                stop_hit = True
                exit_price = active.stop_price
            elif bar.high >= active.tp_price:
                tp_hit = True
                exit_price = active.tp_price
        else:
            if bar.high >= active.stop_price:
                stop_hit = True
                exit_price = active.stop_price
            elif bar.low <= active.tp_price:
                tp_hit = True
                exit_price = active.tp_price

        if stop_hit:
            exit_reason = "orb_stop"
        elif tp_hit:
            exit_reason = "orb_take_profit"
        elif ts_local.timetz().replace(tzinfo=None) >= self._flat_time:
            exit_reason = "orb_flat_time"
            exit_price = bar.close

        if not exit_reason:
            return None

        self._signals_count += 1
        self._exits_count += 1
        self._active = None
        self._last_signal_context = {
            "type": "exit",
            "exit_reason": exit_reason,
            "exit_price": exit_price,
            "range_high": self._range_high,
            "range_low": self._range_low,
            "bar_ts": bar.ts_utc.isoformat(),
            "day": self._current_day.isoformat() if self._current_day else None,
            "range_bars": self._range_bars,
        }
        self._log.info(
            "orb_exit_signal",
            extra={"reason": exit_reason, "exit_price": exit_price, "side": exit_side, "range_high": self._range_high, "range_low": self._range_low},
        )
        return Signal(ts=bar.ts_utc, symbol=self.symbol, side=exit_side, qty=abs(position_qty) or self.cfg.qty, reason=exit_reason)

    def _maybe_move_to_be(self, bar: BarEvent, active: _TradeState) -> None:
        if active.be_trigger_price is None or active.be_armed:
            return
        if active.side == "BUY":
            triggered = bar.high >= active.be_trigger_price
        else:
            triggered = bar.low <= active.be_trigger_price
        if not triggered:
            return
        if active.side == "BUY":
            active.stop_price = active.entry_price + active.be_offset
        else:
            active.stop_price = active.entry_price - active.be_offset
        active.be_armed = True
        self._log.info(
            "orb_be_armed",
            extra={"side": active.side, "stop_price": active.stop_price, "entry_price": active.entry_price, "be_offset": active.be_offset},
        )

    def _build_day_summary(self, day: datetime.date) -> Dict[str, Any]:
        notes = None
        if self._entries_count == 0:
            notes = {"no_trade_reason": "no_entries"}
        return {
            "day": day.isoformat(),
            "timezone": self.cfg.timezone,
            "symbol": self.symbol,
            "strategy": "orb_variant_a",
            "range_start": self.cfg.range_window.start,
            "range_end": self.cfg.range_window.end,
            "entry_start": self.cfg.entry_window.start,
            "entry_end": self.cfg.entry_window.end,
            "range_high": self._range_high,
            "range_low": self._range_low,
            "range_bars": self._range_bars,
            "signals_count": self._signals_count,
            "entries_count": self._entries_count,
            "exits_count": self._exits_count,
            "notes": notes,
        }
