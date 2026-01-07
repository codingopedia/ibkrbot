from __future__ import annotations

from typing import Optional

from trader.events import MarketEvent, Signal
from trader.strategy.base import Strategy


class NoopStrategy(Strategy):
    """Does nothing. Use to validate wiring and logging."""

    def on_market(self, event: MarketEvent) -> Optional[Signal]:
        return None
