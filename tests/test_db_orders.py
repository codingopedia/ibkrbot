from datetime import datetime

from trader.events import OrderIntent
from trader.persistence import Database


def _intent(cid: str = "cid-1") -> OrderIntent:
    return OrderIntent(
        ts=datetime.utcnow(),
        symbol="MGC",
        side="BUY",
        qty=1,
        order_type="MKT",
        limit_price=None,
        client_order_id=cid,
    )


def test_insert_and_update_order(tmp_path) -> None:
    db = Database(str(tmp_path / "db.sqlite"))
    intent = _intent()

    db.insert_order(intent)
    row = db.conn.execute("SELECT status, broker_order_id FROM orders WHERE client_order_id=?", (intent.client_order_id,)).fetchone()
    assert row["status"] == "Created"
    assert row["broker_order_id"] is None

    db.update_order_ack(intent.client_order_id, broker_order_id="B1", status="Submitted")
    row = db.conn.execute("SELECT status, broker_order_id FROM orders WHERE client_order_id=?", (intent.client_order_id,)).fetchone()
    assert row["status"] == "Submitted"
    assert row["broker_order_id"] == "B1"

    db.update_order_status(intent.client_order_id, status="Filled")
    row = db.conn.execute("SELECT status FROM orders WHERE client_order_id=?", (intent.client_order_id,)).fetchone()
    assert row["status"] == "Filled"
