from trader.config import RiskConfig
from trader.ledger import Ledger
from trader.persistence import Database
from trader.reconcile import reconcile_ibkr
from trader.risk import RiskEngine

INSTANCE_ID = "bot1"
PREFIX = f"BOT:{INSTANCE_ID}:"


class _FakeTrade:
    def __init__(self, order_id: str, order_ref: str = ""):
        self.order = type("Order", (), {"orderId": order_id, "orderRef": order_ref})()
        self.orderStatus = type("OrderStatus", (), {"status": "Submitted", "filled": 0, "remaining": 1})()


class _FakePosition:
    def __init__(self, symbol: str, qty: int):
        self.contract = type("Contract", (), {"symbol": symbol})()
        self.position = qty


class _FakeIB:
    def __init__(self, trades=None, positions=None):
        self._open_trades = trades or []
        self._positions = positions or []

    def openTrades(self):
        return self._open_trades

    def positions(self):
        return self._positions


class _FakeBroker:
    def __init__(self, ib: _FakeIB):
        self._ib = ib
        self.cancelled: list[str] = []

    def raw_ib(self):
        return self._ib

    def cancel_order(self, broker_order_id: str) -> None:
        self.cancelled.append(str(broker_order_id))


def _reconcile(broker: _FakeBroker, db: Database, ledger: Ledger, risk: RiskEngine, policy: str, env: str = "paper"):
    reconcile_ibkr(
        broker,
        db,
        ledger,
        risk,
        "MGC",
        instance_id=INSTANCE_ID,
        unknown_orders_policy=policy,
        env=env,
    )


def test_reconcile_marks_missing_on_broker(tmp_path) -> None:
    db = Database(str(tmp_path / "db.sqlite"))
    ledger = Ledger()
    risk = RiskEngine(RiskConfig())

    db.conn.execute(
        """
        INSERT INTO orders(client_order_id, broker_order_id, symbol, side, qty, order_type, limit_price, status, created_ts, updated_ts)
        VALUES('c1','b1','MGC','BUY',1,'MKT',NULL,'Submitted','t','t')
        """
    )
    db.conn.commit()

    broker = _FakeBroker(_FakeIB(trades=[], positions=[]))
    _reconcile(broker, db, ledger, risk, policy="HALT")

    row = db.conn.execute("SELECT status FROM orders WHERE client_order_id='c1'").fetchone()
    assert row["status"] == "MissingOnBroker"
    assert risk.halted is False


def test_reconcile_unknown_orders_policy_halt(tmp_path) -> None:
    db = Database(str(tmp_path / "db.sqlite"))
    ledger = Ledger()
    risk = RiskEngine(RiskConfig())

    trades = [_FakeTrade("999", order_ref="OTHER")]
    broker = _FakeBroker(_FakeIB(trades=trades, positions=[]))
    _reconcile(broker, db, ledger, risk, policy="HALT")

    assert risk.halted is True


def test_reconcile_unknown_orders_policy_ignore(tmp_path) -> None:
    db = Database(str(tmp_path / "db.sqlite"))
    ledger = Ledger()
    risk = RiskEngine(RiskConfig())

    trades = [_FakeTrade("999", order_ref="OTHER")]
    broker = _FakeBroker(_FakeIB(trades=trades, positions=[]))
    _reconcile(broker, db, ledger, risk, policy="IGNORE")

    assert risk.halted is False


def test_reconcile_unknown_orders_policy_cancel_paper(tmp_path) -> None:
    db = Database(str(tmp_path / "db.sqlite"))
    ledger = Ledger()
    risk = RiskEngine(RiskConfig())

    trades = [_FakeTrade("999", order_ref="OTHER")]
    broker = _FakeBroker(_FakeIB(trades=trades, positions=[]))
    _reconcile(broker, db, ledger, risk, policy="CANCEL", env="paper")

    assert broker.cancelled == ["999"]
    assert risk.halted is False


def test_reconcile_unknown_orders_policy_cancel_live_denied(tmp_path) -> None:
    db = Database(str(tmp_path / "db.sqlite"))
    ledger = Ledger()
    risk = RiskEngine(RiskConfig())

    trades = [_FakeTrade("999", order_ref="OTHER")]
    broker = _FakeBroker(_FakeIB(trades=trades, positions=[]))
    _reconcile(broker, db, ledger, risk, policy="CANCEL", env="live")

    assert broker.cancelled == []
    assert risk.halted is True


def test_reconcile_halts_on_position_mismatch(tmp_path) -> None:
    db = Database(str(tmp_path / "db.sqlite"))
    ledger = Ledger()
    risk = RiskEngine(RiskConfig())

    broker = _FakeBroker(_FakeIB(trades=[], positions=[_FakePosition("MGC", 2)]))
    _reconcile(broker, db, ledger, risk, policy="HALT")

    assert risk.halted is True
