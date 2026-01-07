from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

from trader.events import Fill, MarketEvent, OrderAck, OrderIntent, OrderStatusUpdate
from trader.models import Position


class Broker(ABC):
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def place_order(self, intent: OrderIntent) -> OrderAck: ...

    @abstractmethod
    def subscribe_market_data(self, symbol: str, on_event: Callable[[MarketEvent], None]) -> None: ...

    @abstractmethod
    def poll_fills(self, on_fill: Callable[[Fill], None]) -> None:
        """Pull fills/executions and call on_fill for each new fill."""
        raise NotImplementedError

    @abstractmethod
    def poll_order_status(self, on_status: Callable[[OrderStatusUpdate], None]) -> None:
        """Poll order status updates and call on_status."""
        raise NotImplementedError

    @abstractmethod
    def poll_commissions(self, on_commission: Callable[[str, float], None]) -> None:
        """Poll commission backfills and call on_commission(exec_id, commission)."""
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> None: ...

    @abstractmethod
    def cancel_all_orders(self, symbol: Optional[str] = None) -> None: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def flatten(self, symbol: str) -> None: ...
