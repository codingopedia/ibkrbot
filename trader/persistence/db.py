from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from trader.events import BarEvent, Fill, OrderIntent
from trader.models import OrderRecord, PnLSnapshot


class Database:
    def __init__(self, sqlite_path: str) -> None:
        self.path = Path(sqlite_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path.as_posix())
        self.conn.row_factory = sqlite3.Row
        self._apply_schema()

    def _apply_schema(self) -> None:
        schema_path = Path(__file__).with_name("schema.sql")
        self.conn.executescript(schema_path.read_text(encoding="utf-8"))
        # Backfill exec_id column if missing (older DBs) and ensure unique index.
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(fills)")}
        if "exec_id" not in cols:
            try:
                self.conn.execute("ALTER TABLE fills ADD COLUMN exec_id TEXT")
            except sqlite3.OperationalError:
                pass
        # Create a unique index guarding NULLs.
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_exec_id ON fills(exec_id) WHERE exec_id IS NOT NULL"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS bars_1m (ts TEXT NOT NULL, symbol TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL, UNIQUE(symbol, ts))"
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def insert_fill(self, f: Fill) -> None:
        if f.exec_id:
            exists = self.conn.execute("SELECT 1 FROM fills WHERE exec_id=?", (f.exec_id,)).fetchone()
            if exists:
                return
        self.conn.execute(
            """
            INSERT OR IGNORE INTO fills(ts, client_order_id, broker_order_id, exec_id, symbol, side, qty, price, commission)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                f.ts.isoformat(),
                f.client_order_id,
                f.broker_order_id,
                f.exec_id,
                f.symbol,
                f.side,
                f.qty,
                f.price,
                f.commission,
            ),
        )
        self.conn.commit()

    def insert_pnl_snapshot(self, s: PnLSnapshot) -> None:
        self.conn.execute(
            """
            INSERT INTO pnl_snapshots(ts, symbol, position_qty, avg_price, last_price, unrealized_usd, realized_usd, commissions_usd)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                s.ts.isoformat(),
                s.symbol,
                s.position_qty,
                s.avg_price,
                s.last_price,
                s.unrealized_usd,
                s.realized_usd,
                s.commissions_usd,
            ),
        )
        self.conn.commit()

    def insert_order(self, intent: OrderIntent, status: str = "Created") -> None:
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            """
            INSERT OR REPLACE INTO orders(client_order_id, broker_order_id, symbol, side, qty, order_type, limit_price, status, created_ts, updated_ts)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                intent.client_order_id,
                None,
                intent.symbol,
                intent.side,
                intent.qty,
                intent.order_type,
                intent.limit_price,
                status,
                now,
                now,
            ),
        )
        self.conn.commit()

    def update_order_ack(self, client_order_id: str, broker_order_id: str, status: str = "Submitted") -> None:
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            """
            UPDATE orders
            SET broker_order_id = ?, status = ?, updated_ts = ?
            WHERE client_order_id = ?
            """,
            (broker_order_id, status, now, client_order_id),
        )
        self.conn.commit()

    def update_order_status(self, client_order_id: str, status: str, broker_order_id: Optional[str] = None) -> None:
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            """
            UPDATE orders
            SET status = ?, broker_order_id = COALESCE(?, broker_order_id), updated_ts = ?
            WHERE client_order_id = ?
            """,
            (status, broker_order_id, now, client_order_id),
        )
        self.conn.commit()

    def update_fill_commission(self, exec_id: str, commission: float) -> bool:
        cur = self.conn.execute(
            """
            UPDATE fills
            SET commission = ?
            WHERE exec_id = ? AND (commission IS NULL OR commission = 0)
            """,
            (commission, exec_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def upsert_bar(self, bar: BarEvent) -> None:
        self.conn.execute(
            """
            INSERT INTO bars_1m(ts, symbol, open, high, low, close, volume)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(symbol, ts) DO UPDATE SET
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume
            """,
            (
                bar.ts_utc.isoformat(),
                bar.symbol,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
            ),
        )
        self.conn.commit()

    def get_open_orders(self) -> list[OrderRecord]:
        rows = self.conn.execute(
            """
            SELECT client_order_id, broker_order_id, symbol, side, qty, order_type, limit_price, status, created_ts, updated_ts
            FROM orders
            WHERE status NOT IN ('Filled', 'Cancelled', 'MissingOnBroker')
            """
        ).fetchall()
        return [
            OrderRecord(
                client_order_id=row["client_order_id"],
                broker_order_id=row["broker_order_id"],
                symbol=row["symbol"],
                side=row["side"],
                qty=row["qty"],
                order_type=row["order_type"],
                limit_price=row["limit_price"],
                status=row["status"],
                created_ts=row["created_ts"],
                updated_ts=row["updated_ts"],
            )
            for row in rows
        ]

    def insert_signal(
        self,
        *,
        ts: datetime,
        symbol: str,
        strategy: str,
        type: str,
        side: Optional[str] = None,
        qty: Optional[int] = None,
        reason: str = "",
        price_ref: Optional[float] = None,
        bar_ts: Optional[datetime] = None,
        is_snapshot: bool = False,
        extras: Optional[dict] = None,
    ) -> None:
        extras_json = json.dumps(extras or {}, ensure_ascii=True)
        self.conn.execute(
            """
            INSERT INTO strategy_signals(ts, symbol, strategy, type, side, qty, reason, price_ref, bar_ts, is_snapshot, extras_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ts.isoformat(),
                symbol,
                strategy,
                type,
                side,
                qty,
                reason,
                price_ref,
                bar_ts.isoformat() if bar_ts else None,
                1 if is_snapshot else 0,
                extras_json,
            ),
        )
        self.conn.commit()

    def upsert_trade(
        self,
        *,
        trade_id: str,
        instance_id: str,
        strategy: str,
        symbol: str,
        entry_ts: Optional[datetime],
        entry_price: Optional[float],
        entry_side: Optional[str],
        entry_reason: Optional[str],
        exit_ts: Optional[datetime],
        exit_price: Optional[float],
        exit_reason: Optional[str],
        qty: Optional[int],
        pnl_usd: Optional[float],
        range_high: Optional[float],
        range_low: Optional[float],
        risk_per_unit: Optional[float],
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO trade_journal(trade_id, instance_id, strategy, symbol, entry_ts, entry_price, entry_side, entry_reason, exit_ts, exit_price, exit_reason, qty, pnl_usd, range_high, range_low, risk_per_unit)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(trade_id) DO UPDATE SET
                instance_id=excluded.instance_id,
                strategy=excluded.strategy,
                symbol=excluded.symbol,
                entry_ts=excluded.entry_ts,
                entry_price=excluded.entry_price,
                entry_side=excluded.entry_side,
                entry_reason=excluded.entry_reason,
                exit_ts=excluded.exit_ts,
                exit_price=excluded.exit_price,
                exit_reason=excluded.exit_reason,
                qty=excluded.qty,
                pnl_usd=excluded.pnl_usd,
                range_high=excluded.range_high,
                range_low=excluded.range_low,
                risk_per_unit=excluded.risk_per_unit
            """,
            (
                trade_id,
                instance_id,
                strategy,
                symbol,
                entry_ts.isoformat() if entry_ts else None,
                entry_price,
                entry_side,
                entry_reason,
                exit_ts.isoformat() if exit_ts else None,
                exit_price,
                exit_reason,
                qty,
                pnl_usd,
                range_high,
                range_low,
                risk_per_unit,
            ),
        )
        self.conn.commit()

    def upsert_trade_metrics(
        self, *, trade_id: str, duration_seconds: Optional[float], mfe: Optional[float], mae: Optional[float], r_multiple: Optional[float]
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO trade_metrics(trade_id, duration_seconds, mfe, mae, r_multiple)
            VALUES(?,?,?,?,?)
            ON CONFLICT(trade_id) DO UPDATE SET
                duration_seconds=excluded.duration_seconds,
                mfe=excluded.mfe,
                mae=excluded.mae,
                r_multiple=excluded.r_multiple
            """,
            (trade_id, duration_seconds, mfe, mae, r_multiple),
        )
        self.conn.commit()

    def upsert_trade_shadow(
        self, *, trade_id: str, variant_name: str, exit_ts: Optional[datetime], exit_price: Optional[float], pnl_usd: Optional[float], reason_exit: str
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO trade_shadow(trade_id, variant_name, exit_ts, exit_price, pnl_usd, reason_exit)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(trade_id, variant_name) DO UPDATE SET
                exit_ts=excluded.exit_ts,
                exit_price=excluded.exit_price,
                pnl_usd=excluded.pnl_usd,
                reason_exit=excluded.reason_exit
            """,
            (
                trade_id,
                variant_name,
                exit_ts.isoformat() if exit_ts else None,
                exit_price,
                pnl_usd,
                reason_exit,
            ),
        )
        self.conn.commit()

    def get_bars_between(self, symbol: str, start: datetime, end: datetime) -> list[BarEvent]:
        rows = self.conn.execute(
            """
            SELECT ts, open, high, low, close, volume FROM bars_1m
            WHERE symbol = ? AND ts >= ? AND ts <= ?
            ORDER BY ts ASC
            """,
            (symbol, start.isoformat(), end.isoformat()),
        ).fetchall()
        result: list[BarEvent] = []
        for row in rows:
            try:
                ts = datetime.fromisoformat(row["ts"])
            except Exception:
                continue
            result.append(
                BarEvent(
                    ts_utc=ts,
                    symbol=symbol,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
            )
        return result

    def upsert_strategy_daily(
        self,
        *,
        day: str,
        symbol: str,
        strategy: str,
        timezone: str,
        range_start: Optional[str],
        range_end: Optional[str],
        entry_start: Optional[str],
        entry_end: Optional[str],
        range_high: Optional[float],
        range_low: Optional[float],
        range_bars: int,
        signals_count: int,
        entries_count: int,
        exits_count: int,
        trades_closed_count: int,
        notes_json: Optional[str],
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO strategy_daily(day, symbol, strategy, timezone, range_start, range_end, entry_start, entry_end, range_high, range_low, range_bars, signals_count, entries_count, exits_count, trades_closed_count, notes_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(day, symbol, strategy) DO UPDATE SET
                timezone=excluded.timezone,
                range_start=excluded.range_start,
                range_end=excluded.range_end,
                entry_start=excluded.entry_start,
                entry_end=excluded.entry_end,
                range_high=excluded.range_high,
                range_low=excluded.range_low,
                range_bars=excluded.range_bars,
                signals_count=excluded.signals_count,
                entries_count=excluded.entries_count,
                exits_count=excluded.exits_count,
                trades_closed_count=excluded.trades_closed_count,
                notes_json=excluded.notes_json
            """,
            (
                day,
                symbol,
                strategy,
                timezone,
                range_start,
                range_end,
                entry_start,
                entry_end,
                range_high,
                range_low,
                range_bars,
                signals_count,
                entries_count,
                exits_count,
                trades_closed_count,
                notes_json,
            ),
        )
        self.conn.commit()
