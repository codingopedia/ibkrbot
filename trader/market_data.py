from __future__ import annotations

import math
from typing import Optional

from trader.events import MarketEvent


def normalize_price(value: Optional[float]) -> Optional[float]:
    """Convert IBKR tick values to float, mapping NaN or invalid inputs to None."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def is_valid_price(value: Optional[float]) -> bool:
    """Return True when price is a real number (not None/NaN)."""
    if value is None:
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return not math.isnan(number)


def is_valid_tick(evt: MarketEvent) -> bool:
    """A market tick is valid when at least one price field is a real number."""
    return any(is_valid_price(price) for price in (evt.bid, evt.ask, evt.last))
