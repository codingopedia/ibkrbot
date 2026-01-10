from datetime import datetime, timedelta, timezone

from ib_insync import BarData

from trader.broker.ibkr.adapter import IBKRBroker


def test_ibkr_snapshot_emits_unique_bars_and_sets_utc() -> None:
    broker = IBKRBroker()
    emitted = []

    broker._bars_cb = lambda evt: emitted.append(evt)  # type: ignore[attr-defined]

    ts_eastern = datetime(2024, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=-5)))
    ts_utc = datetime(2024, 1, 1, 17, 0, tzinfo=timezone.utc)
    bars = [
        BarData(date=ts_eastern, open=1.0, high=2.0, low=0.5, close=1.5, volume=10, average=1.5, barCount=10),
        BarData(date=ts_utc, open=2.0, high=2.5, low=1.5, close=2.2, volume=15, average=2.0, barCount=8),
    ]

    emitted_count = broker._emit_bars_snapshot("MGC", bars)

    assert emitted_count == 1
    assert len(emitted) == 1

    evt = emitted[0]
    assert evt.symbol == "MGC"
    assert evt.ts_utc.tzinfo == timezone.utc
    assert evt.ts_utc.hour == 17
    assert evt.close == 1.5
    assert ("MGC", evt.ts_utc.isoformat()) in broker._seen_bar_keys
