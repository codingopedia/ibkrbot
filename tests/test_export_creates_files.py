from datetime import datetime, timezone

import yaml
from typer.testing import CliRunner

from trader.events import BarEvent
from trader.main import app
from trader.persistence.db import Database


def test_export_creates_files(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite"
    cfg_path = tmp_path / "cfg.yaml"
    outdir = tmp_path / "exports"

    cfg_path.write_text(
        yaml.safe_dump(
            {
                "env": "paper",
                "storage": {"sqlite_path": str(db_path)},
                "strategy": {"type": "orb_variant_a"},
                "instrument": {"symbol": "ES"},
            }
        ),
        encoding="utf-8",
    )

    db = Database(db_path.as_posix())
    bar = BarEvent(
        ts_utc=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
        symbol="ES",
        open=100.0,
        high=105.0,
        low=95.0,
        close=102.0,
        volume=10.0,
    )
    db.upsert_bar(bar)
    now = datetime.utcnow()
    db.insert_signal(
        ts=now,
        symbol="ES",
        strategy="orb_variant_a",
        type="entry",
        side="BUY",
        qty=1,
        reason="test",
        price_ref=102.0,
        bar_ts=bar.ts_utc,
        is_snapshot=False,
        extras={"foo": "bar"},
    )
    db.upsert_trade(
        trade_id="t1",
        instance_id="bot1",
        strategy="orb_variant_a",
        symbol="ES",
        entry_ts=now,
        entry_price=102.0,
        entry_side="BUY",
        entry_reason="test",
        exit_ts=now,
        exit_price=104.0,
        exit_reason="exit",
        qty=1,
        pnl_usd=100.0,
        range_high=105.0,
        range_low=95.0,
        risk_per_unit=2.0,
    )
    db.upsert_trade_metrics(trade_id="t1", duration_seconds=60.0, mfe=5.0, mae=1.0, r_multiple=1.0)
    db.upsert_trade_shadow(
        trade_id="t1",
        variant_name="B_no_BE_bigger_TP",
        exit_ts=now,
        exit_price=105.0,
        pnl_usd=150.0,
        reason_exit="tp",
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
        range_low=95.0,
        range_bars=1,
        signals_count=1,
        entries_count=1,
        exits_count=0,
        trades_closed_count=1,
        notes_json=None,
    )

    runner = CliRunner()
    result = runner.invoke(app, ["export", "-c", str(cfg_path), "--outdir", str(outdir), "--days", "30"])
    assert result.exit_code == 0, result.output

    files = list(outdir.glob("*.csv"))
    assert files, "expected CSV exports"
    names = [p.name for p in files]
    assert any("strategy_signals" in name for name in names)
    assert any("trade_journal" in name for name in names)
    assert any("strategy_daily" in name for name in names)
