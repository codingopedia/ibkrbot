from trader import main
from trader.events import Signal


def test_run_does_not_place_orders_when_trading_disabled(tmp_path, monkeypatch) -> None:
    cfg_path = tmp_path / "config.yaml"
    db_path = tmp_path / "db.sqlite"
    cfg_path.write_text(
        f"""
env: paper
runtime:
  heartbeat_seconds: 0
  instance_id: t1
trading:
  enabled: false
  allow_live: false
storage:
  sqlite_path: "{db_path.as_posix()}"
broker:
  type: sim
instrument:
  symbol: ES
  exchange: GLOBEX
  currency: USD
  multiplier: 50.0
orders:
  default_tif: DAY
  outside_rth: false
risk:
  max_position: 1
  max_daily_loss_usd: 100.0
  max_order_size: 1
""",
        encoding="utf-8",
    )

    called = {"count": 0}

    def fake_place_order(self, intent):
        called["count"] += 1
        raise AssertionError("place_order should not be called when trading is disabled")

    monkeypatch.setattr(main.SimBroker, "place_order", fake_place_order)

    class _FakeStrategy:
        def __init__(self) -> None:
            self._emitted = False

        def on_market(self, event):
            if self._emitted:
                return None
            self._emitted = True
            return Signal(ts=event.ts, symbol=event.symbol, side="BUY", qty=1)

    monkeypatch.setattr(main, "NoopStrategy", _FakeStrategy)

    main.run(config=str(cfg_path), iterations=1)

    assert called["count"] == 0
