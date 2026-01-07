from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional


@dataclass
class Position:
    symbol: str
    qty: int = 0
    avg_price: float = 0.0


@dataclass
class PnLSnapshot:
    ts: datetime
    symbol: str
    position_qty: int
    avg_price: float
    last_price: Optional[float]
    unrealized_usd: float
    realized_usd: float
    commissions_usd: float


@dataclass
class OrderRecord:
    client_order_id: str
    broker_order_id: Optional[str]
    symbol: str
    side: str
    qty: int
    order_type: str
    limit_price: Optional[float]
    status: str
    created_ts: str
    updated_ts: str
