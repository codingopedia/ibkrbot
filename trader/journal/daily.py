from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from trader.persistence.db import Database


class StrategyDailyAggregator:
    """Aggregate per-day strategy metadata and persist to SQLite."""

    def __init__(
        self,
        db: Database,
        *,
        symbol: str,
        strategy: str,
        timezone: str,
        range_start: Optional[str] = None,
        range_end: Optional[str] = None,
        entry_start: Optional[str] = None,
        entry_end: Optional[str] = None,
    ) -> None:
        self.db = db
        self.base = {
            "symbol": symbol,
            "strategy": strategy,
            "timezone": timezone,
            "range_start": range_start,
            "range_end": range_end,
            "entry_start": entry_start,
            "entry_end": entry_end,
        }
        self.state: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    def _get_record(self, day: str) -> Dict[str, Any]:
        key = (day, self.base["symbol"], self.base["strategy"])
        if key not in self.state:
            self.state[key] = {
                "day": day,
                **self.base,
                "range_high": None,
                "range_low": None,
                "range_bars": 0,
                "signals_count": 0,
                "entries_count": 0,
                "exits_count": 0,
                "trades_closed_count": 0,
                "notes_json": None,
            }
        return self.state[key]

    def update_state(
        self,
        *,
        day: str,
        timezone: Optional[str] = None,
        symbol: Optional[str] = None,
        strategy: Optional[str] = None,
        range_start: Optional[str] = None,
        range_end: Optional[str] = None,
        entry_start: Optional[str] = None,
        entry_end: Optional[str] = None,
        range_high: Optional[float] = None,
        range_low: Optional[float] = None,
        range_bars: Optional[int] = None,
        signals_count: Optional[int] = None,
        entries_count: Optional[int] = None,
        exits_count: Optional[int] = None,
        trades_closed_count: Optional[int] = None,
        notes: Optional[dict] = None,
    ) -> None:
        rec = self._get_record(day)
        if timezone:
            rec["timezone"] = timezone
        if symbol:
            rec["symbol"] = symbol
        if strategy:
            rec["strategy"] = strategy
        rec["range_start"] = range_start or rec.get("range_start")
        rec["range_end"] = range_end or rec.get("range_end")
        rec["entry_start"] = entry_start or rec.get("entry_start")
        rec["entry_end"] = entry_end or rec.get("entry_end")
        if range_high is not None:
            rec["range_high"] = range_high
        if range_low is not None:
            rec["range_low"] = range_low
        if range_bars is not None:
            rec["range_bars"] = max(rec.get("range_bars", 0), range_bars)
        if signals_count is not None:
            rec["signals_count"] = max(rec.get("signals_count", 0), signals_count)
        if entries_count is not None:
            rec["entries_count"] = max(rec.get("entries_count", 0), entries_count)
        if exits_count is not None:
            rec["exits_count"] = max(rec.get("exits_count", 0), exits_count)
        if trades_closed_count is not None:
            rec["trades_closed_count"] = max(rec.get("trades_closed_count", 0), trades_closed_count)
        if notes is not None:
            rec["notes_json"] = json.dumps(notes, ensure_ascii=True)

        self._persist(rec)

    def bump_trade_closed(self, day: str, count: int = 1) -> None:
        rec = self._get_record(day)
        rec["trades_closed_count"] = rec.get("trades_closed_count", 0) + count
        self._persist(rec)

    def _persist(self, rec: Dict[str, Any]) -> None:
        self.db.upsert_strategy_daily(
            day=rec["day"],
            symbol=rec["symbol"],
            strategy=rec["strategy"],
            timezone=rec["timezone"],
            range_start=rec.get("range_start"),
            range_end=rec.get("range_end"),
            entry_start=rec.get("entry_start"),
            entry_end=rec.get("entry_end"),
            range_high=rec.get("range_high"),
            range_low=rec.get("range_low"),
            range_bars=int(rec.get("range_bars", 0)),
            signals_count=int(rec.get("signals_count", 0)),
            entries_count=int(rec.get("entries_count", 0)),
            exits_count=int(rec.get("exits_count", 0)),
            trades_closed_count=int(rec.get("trades_closed_count", 0)),
            notes_json=rec.get("notes_json"),
        )
