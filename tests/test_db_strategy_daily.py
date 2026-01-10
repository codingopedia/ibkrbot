from datetime import datetime

from trader.persistence.db import Database


def test_upsert_strategy_daily_overwrites(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite"
    db = Database(db_path.as_posix())

    db.upsert_strategy_daily(
        day="2024-01-01",
        symbol="ES",
        strategy="orb_variant_a",
        timezone="UTC",
        range_start="00:00",
        range_end="05:00",
        entry_start="07:00",
        entry_end="10:00",
        range_high=100.0,
        range_low=90.0,
        range_bars=3,
        signals_count=1,
        entries_count=1,
        exits_count=0,
        trades_closed_count=0,
        notes_json=None,
    )

    db.upsert_strategy_daily(
        day="2024-01-01",
        symbol="ES",
        strategy="orb_variant_a",
        timezone="UTC",
        range_start="00:00",
        range_end="05:00",
        entry_start="07:00",
        entry_end="10:00",
        range_high=105.0,
        range_low=89.0,
        range_bars=5,
        signals_count=2,
        entries_count=1,
        exits_count=1,
        trades_closed_count=1,
        notes_json='{"no_trade_reason": "none"}',
    )

    row = db.conn.execute(
        "SELECT range_high, range_low, range_bars, signals_count, entries_count, exits_count, trades_closed_count, notes_json FROM strategy_daily WHERE day='2024-01-01'"
    ).fetchone()
    assert row["range_high"] == 105.0
    assert row["range_low"] == 89.0
    assert row["range_bars"] == 5
    assert row["signals_count"] == 2
    assert row["entries_count"] == 1
    assert row["exits_count"] == 1
    assert row["trades_closed_count"] == 1
    assert row["notes_json"] == '{"no_trade_reason": "none"}'
