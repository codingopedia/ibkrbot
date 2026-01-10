import logging
from datetime import datetime

from trader.config import CustomSpecStrategyConfig
from trader.events import BarEvent
from trader.strategy.custom_spec import CustomSpecStrategy


def _bar(ts_minute: int) -> BarEvent:
    return BarEvent(
        ts_utc=datetime(2024, 1, 1, 12, ts_minute, 0),
        symbol="MGC",
        open=1.0,
        high=1.1,
        low=0.9,
        close=1.05,
        volume=100,
    )


def test_custom_spec_warmup_tracking_and_no_signals() -> None:
    cfg = CustomSpecStrategyConfig(min_warmup_bars=3)
    strat = CustomSpecStrategy(cfg, logger=logging.getLogger("test.custom_spec"))

    assert strat.warmup_done is False

    for i in range(5):
        sig = strat.on_bar(_bar(i), position_qty=0)
        assert sig is None

    assert strat.bars_seen == 5
    assert strat.warmup_done is True


def test_custom_spec_warmup_threshold() -> None:
    cfg = CustomSpecStrategyConfig(min_warmup_bars=2)
    strat = CustomSpecStrategy(cfg)

    strat.on_bar(_bar(0), position_qty=0)
    assert strat.warmup_done is False

    strat.on_bar(_bar(1), position_qty=0)
    assert strat.warmup_done is True

    strat.on_bar(_bar(2), position_qty=0)
    assert strat.warmup_done is True
