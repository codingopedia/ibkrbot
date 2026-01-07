from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from trader.events import Fill, OrderIntent
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
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def insert_fill(self, f: Fill) -> None:
        self.conn.execute(
            """
            INSERT INTO fills(ts, client_order_id, broker_order_id, exec_id, symbol, side, qty, price, commission)
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
