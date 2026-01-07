from trader.config import RiskConfig
from trader.ledger import Ledger
from trader.persistence import Database
from trader.reconcile import reconcile_ibkr
from trader.risk import RiskEngine


class _FakeTrade:
    def __init__(self, order_id: str):
        self.order = type("Order", (), {"orderId": order_id})()
        self.orderStatus = type("OrderStatus", (), {"status": "Submitted", "filled": 0, "remaining": 1})()


class _FakePosition:
    def __init__(self, symbol: str, qty: int):
        self.contract = type("Contract", (), {"symbol": symbol})()
        self.position = qty


class _FakeIB:
    def __init__(self, open_ids=None, positions=None):
        self._open_trades = [_FakeTrade(oid) for oid in (open_ids or [])]
        self._positions = positions or []

    def openTrades(self):
        return self._open_trades

    def positions(self):
        return self._positions


class _FakeBroker:
    def __init__(self, ib: _FakeIB):
        self._ib = ib

    def raw_ib(self):
        return self._ib


def test_reconcile_marks_missing_on_broker(tmp_path) -> None:
    db = Database(str(tmp_path / "db.sqlite"))
    ledger = Ledger()
    risk = RiskEngine(RiskConfig())

    # DB has open order with broker id, IB has none.
    db.conn.execute(
        """
        INSERT INTO orders(client_order_id, broker_order_id, symbol, side, qty, order_type, limit_price, status, created_ts, updated_ts)
        VALUES('c1','b1','MGC','BUY',1,'MKT',NULL,'Submitted','t','t')
        """
    )
    db.conn.commit()

    broker = _FakeBroker(_FakeIB(open_ids=[], positions=[]))
    reconcile_ibkr(broker, db, ledger, risk, "MGC")

    row = db.conn.execute("SELECT status FROM orders WHERE client_order_id='c1'").fetchone()
    assert row["status"] == "MissingOnBroker"
    assert risk.halted is False


def test_reconcile_halts_on_unknown_ib_order(tmp_path) -> None:
    db = Database(str(tmp_path / "db.sqlite"))
    ledger = Ledger()
    risk = RiskEngine(RiskConfig())

    broker = _FakeBroker(_FakeIB(open_ids=["999"], positions=[]))
    reconcile_ibkr(broker, db, ledger, risk, "MGC")

    assert risk.halted is True


def test_reconcile_halts_on_position_mismatch(tmp_path) -> None:
    db = Database(str(tmp_path / "db.sqlite"))
    ledger = Ledger()
    risk = RiskEngine(RiskConfig())

    broker = _FakeBroker(_FakeIB(open_ids=[], positions=[_FakePosition("MGC", 2)]))
    reconcile_ibkr(broker, db, ledger, risk, "MGC")

    assert risk.halted is True
