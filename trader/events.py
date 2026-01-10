from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional


@dataclass(frozen=True)
class MarketEvent:
    ts: datetime
    symbol: str
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]


@dataclass(frozen=True)
class BarEvent:
    ts_utc: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_snapshot: bool = False


@dataclass(frozen=True)
class Signal:
    ts: datetime
    symbol: str
    side: Literal["BUY", "SELL"]
    qty: int
    reason: str = ""


@dataclass(frozen=True)
class OrderIntent:
    ts: datetime
    symbol: str
    side: Literal["BUY", "SELL"]
    qty: int
    order_type: Literal["MKT", "LMT"] = "MKT"
    limit_price: Optional[float] = None
    client_order_id: str = ""


@dataclass(frozen=True)
class OrderAck:
    ts: datetime
    client_order_id: str
    broker_order_id: str
    status: str


@dataclass(frozen=True)
class Fill:
    ts: datetime
    client_order_id: str
    broker_order_id: str
    exec_id: Optional[str]
    symbol: str
    side: Literal["BUY", "SELL"]
    qty: int
    price: float
    commission: float = 0.0


@dataclass(frozen=True)
class OrderStatusUpdate:
    ts: datetime
    client_order_id: str
    broker_order_id: str
    status: str
    filled: Optional[float] = None
    remaining: Optional[float] = None
