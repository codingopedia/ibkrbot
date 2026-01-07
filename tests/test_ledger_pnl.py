from __future__ import annotations

from datetime import datetime

from trader.events import Fill
from trader.ledger.ledger import Ledger


def test_ledger_realized_unrealized_long_roundtrip() -> None:
    l = Ledger()
    # Buy 2 at 100
    l.on_fill(Fill(ts=datetime.utcnow(), client_order_id="c1", broker_order_id="b1", exec_id=None, symbol="MGC", side="BUY", qty=2, price=100.0, commission=0.0))
    snap = l.snapshot("MGC", last_price=105.0)
    assert snap.unrealized_usd == (105.0 - 100.0) * 2
    assert snap.realized_usd == 0.0

    # Sell 2 at 110 -> realize
    l.on_fill(Fill(ts=datetime.utcnow(), client_order_id="c2", broker_order_id="b2", exec_id=None, symbol="MGC", side="SELL", qty=2, price=110.0, commission=0.0))
    snap2 = l.snapshot("MGC", last_price=110.0)
    assert snap2.position_qty == 0
    assert snap2.realized_usd == (110.0 - 100.0) * 2
    assert snap2.unrealized_usd == 0.0


def test_ledger_short_unrealized() -> None:
    l = Ledger()
    # Sell 1 at 200 (short)
    l.on_fill(Fill(ts=datetime.utcnow(), client_order_id="c1", broker_order_id="b1", exec_id=None, symbol="MGC", side="SELL", qty=1, price=200.0, commission=0.0))
    snap = l.snapshot("MGC", last_price=190.0)
    assert snap.unrealized_usd == (200.0 - 190.0) * 1
