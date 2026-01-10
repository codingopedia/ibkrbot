from datetime import datetime, timezone

from trader.config import BreakEvenConfig, ShadowVariantsConfig, StrategyORBVariantAConfig, TimeWindowConfig
from trader.events import BarEvent
from trader.strategy.orb_variant_a import ORBVariantAStrategy


def _bar(minute: int, open_: float, high: float, low: float, close: float, snapshot: bool = False) -> BarEvent:
    ts = datetime(2024, 1, 1, 0, minute, tzinfo=timezone.utc)
    return BarEvent(ts_utc=ts, symbol="ES", open=open_, high=high, low=low, close=close, volume=1000, is_snapshot=snapshot)


def _default_cfg() -> StrategyORBVariantAConfig:
    return StrategyORBVariantAConfig(
        timezone="UTC",
        allow_short=True,
        max_trades_per_day=2,
        min_tick=0.1,
        range_window=TimeWindowConfig(start="00:00", end="00:02"),
        entry_window=TimeWindowConfig(start="00:03", end="00:10"),
        flat_time="00:05",
        breakout_buffer_ticks=1.0,
        sl_buffer_ticks=1.0,
        tp_r=2.0,
        be=BreakEvenConfig(enabled=True, trigger_r=1.0, offset_ticks=0.0),
        shadow_variants=ShadowVariantsConfig(enabled=True, variant_b_tp_r=3.0, variant_c_sl_tight_r=0.5),
        qty=1,
    )


def test_orb_builds_range_and_enters_in_window() -> None:
    strat = ORBVariantAStrategy(_default_cfg(), symbol="ES")

    # Build range from snapshot and normal bars
    assert strat.on_bar(_bar(0, 100, 101, 99, 100.5, snapshot=True), position_qty=0) is None
    assert strat.on_bar(_bar(1, 100.2, 101.5, 99.2, 100.7), position_qty=0) is None

    # Before entry window -> no signal even if price breaks
    assert strat.on_bar(_bar(2, 102, 102, 101.8, 102.0), position_qty=0) is None

    # In entry window -> breakout triggers long
    sig = strat.on_bar(_bar(3, 102, 103, 101.9, 103.0), position_qty=0)
    assert sig is not None
    assert sig.side == "BUY"
    assert sig.reason == "orb_entry_long"

    ctx = strat.get_signal_context()
    assert ctx.get("type") == "entry"
    assert ctx.get("range_high") >= 101.5
    assert ctx.get("range_low") <= 99.2


def test_orb_flat_time_exit() -> None:
    cfg = _default_cfg()
    strat = ORBVariantAStrategy(cfg, symbol="ES")
    # Build range and enter
    strat.on_bar(_bar(0, 100, 101, 99, 100.5), position_qty=0)
    strat.on_bar(_bar(1, 100.5, 101.5, 99.5, 101.0), position_qty=0)
    entry_sig = strat.on_bar(_bar(3, 102, 103, 101.8, 103.0), position_qty=0)
    assert entry_sig is not None

    # Force flat at configured time
    exit_sig = strat.on_bar(_bar(5, 103, 104, 102.5, 103.2), position_qty=1)
    assert exit_sig is not None
    assert exit_sig.reason == "orb_flat_time"
    assert exit_sig.side == "SELL"
