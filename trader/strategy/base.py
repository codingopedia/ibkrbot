from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from trader.events import MarketEvent, Signal


class Strategy(ABC):
    @abstractmethod
    def on_market(self, event: MarketEvent) -> Optional[Signal]:
        """Return a Signal or None."""
        raise NotImplementedError
