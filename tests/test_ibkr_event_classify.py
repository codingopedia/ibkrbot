import logging

from trader.broker.ibkr.adapter import classify_ibkr_event


def test_classify_ibkr_event_maps_ok_and_warning_and_error() -> None:
    assert classify_ibkr_event(2104, "Market data farm connection is OK") == logging.INFO
    assert classify_ibkr_event(10349, "Order rejected") == logging.WARNING
    assert classify_ibkr_event(9999, "Some error") == logging.ERROR
