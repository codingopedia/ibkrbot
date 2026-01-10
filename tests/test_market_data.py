from datetime import datetime

from trader.events import MarketEvent
from trader.market_data import is_valid_price, is_valid_tick, normalize_price


def test_normalize_price_maps_nan_to_none() -> None:
    assert normalize_price(float("nan")) is None
    assert normalize_price(None) is None
    assert normalize_price(101.5) == 101.5


def test_is_valid_price_rejects_nan_and_none() -> None:
    assert not is_valid_price(float("nan"))
    assert not is_valid_price(None)
    assert is_valid_price(0.0)


def test_is_valid_tick_requires_any_price() -> None:
    empty = MarketEvent(ts=datetime.utcnow(), symbol="ES", bid=None, ask=None, last=None)
    partial = MarketEvent(ts=datetime.utcnow(), symbol="ES", bid=4100.0, ask=None, last=None)

    assert not is_valid_tick(empty)
    assert is_valid_tick(partial)
