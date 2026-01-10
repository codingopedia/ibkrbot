from datetime import datetime, timezone

from trader.events import BarEvent
from trader.journal.analytics import compute_shadow_exits


def _bar(ts_minute: int, open_: float, high: float, low: float, close: float) -> BarEvent:
    ts = datetime(2024, 1, 1, 0, ts_minute, tzinfo=timezone.utc)
    return BarEvent(ts_utc=ts, symbol="ES", open=open_, high=high, low=low, close=close, volume=1000)


def test_shadow_variants_exits() -> None:
    bars = [
        _bar(0, 100, 103, 97, 102),  # triggers tighter SL (97.5) but not base stop (95)
        _bar(1, 102, 116, 101, 115),  # hits variant B TP (entry + 3R)
    ]

    results = compute_shadow_exits(
        trade_id="t1",
        bars=bars,
        side="BUY",
        entry_price=100.0,
        qty=1,
        price_multiplier=1.0,
        risk_per_unit=5.0,
        stop_price=95.0,
        tp_price=110.0,
        flat_time=None,
        tz_name="UTC",
        variant_b_tp_r=3.0,
        variant_c_sl_tight_r=0.5,
    )

    variant_b = next(res for res in results if res.variant_name.startswith("B"))
    variant_c = next(res for res in results if res.variant_name.startswith("C"))

    assert variant_b.reason_exit == "tp"
    assert variant_b.exit_price == 115.0

    assert variant_c.reason_exit == "stop"
    assert variant_c.exit_price == 97.5
