from trader.config import AppConfig, OrderConfig


def test_orders_defaults_present() -> None:
    cfg = AppConfig()
    assert cfg.orders.default_tif == "DAY"
    assert cfg.orders.outside_rth is False


def test_orders_override_from_dict() -> None:
    cfg = AppConfig.model_validate(
        {
            "orders": {
                "default_tif": "GTC",
                "outside_rth": True,
            }
        }
    )
    assert cfg.orders.default_tif == "GTC"
    assert cfg.orders.outside_rth is True
