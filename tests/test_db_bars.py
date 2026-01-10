from datetime import datetime

from trader.events import BarEvent
from trader.persistence import Database


def test_upsert_bar_idempotent(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite"
    db = Database(db_path.as_posix())

    bar = BarEvent(
        ts_utc=datetime(2024, 1, 1, 12, 0, 0),
        symbol="ES",
        open=100.0,
        high=101.0,
        low=99.5,
        close=100.5,
        volume=123.0,
    )

    db.upsert_bar(bar)
    db.upsert_bar(bar)

    rows = db.conn.execute("SELECT COUNT(*) as cnt FROM bars_1m").fetchone()
    assert rows["cnt"] == 1
