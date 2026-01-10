from pathlib import Path
import logging

import pytest

from trader.config import AppConfig, load_config


SAMPLE_CONFIGS = [
    Path(__file__).resolve().parent.parent / "config" / "paper.example.yaml",
    Path(__file__).resolve().parent.parent / "config" / "paper.ibkr.example.yaml",
    Path(__file__).resolve().parent.parent / "config" / "paper.tws.bars.local.yaml",
    Path(__file__).resolve().parent.parent / "config" / "paper.tws.custom.local.yaml",
    Path(__file__).resolve().parent.parent / "config" / "paper.tws.orb_a.local.yaml",
]


def _existing_sample_configs() -> list[Path]:
    return [path for path in SAMPLE_CONFIGS if path.exists()]


def test_sample_configs_parse() -> None:
    paths = _existing_sample_configs()
    if not paths:
        pytest.skip("Sample config files are missing")

    for path in paths:
        cfg = load_config(path)
        assert isinstance(cfg, AppConfig)
        assert cfg.strategy is not None
        assert cfg.trading is not None
        assert cfg.backtest is not None


def test_instrument_subconfigs_promoted(caplog: pytest.LogCaptureFixture) -> None:
    cfg_data = {
        "instrument": {
            "symbol": "ES",
            "exchange": "CME",
            "currency": "USD",
            "multiplier": 50.0,
            "strategy": {"type": "ema_cross"},
            "backtest": {
                "fill_model": "close",
                "commission_per_contract_usd": 2.5,
                "slippage_ticks": 1,
                "min_tick": 0.25,
            },
            "trading": {"enabled": True, "allow_live": True},
        }
    }

    with caplog.at_level(logging.WARNING, logger="trader.config"):
        cfg = AppConfig.model_validate(cfg_data)

    assert cfg.strategy.type == "ema_cross"
    assert cfg.backtest.fill_model == "close"
    assert cfg.backtest.commission_per_contract_usd == 2.5
    assert cfg.backtest.slippage_ticks == 1
    assert cfg.backtest.min_tick == 0.25
    assert cfg.trading.enabled is True
    assert cfg.trading.allow_live is True
    assert cfg.instrument.symbol == "ES"
    assert any("Promoted instrument." in message for message in caplog.messages)


def test_instrument_subconfigs_conflict_ignored(caplog: pytest.LogCaptureFixture) -> None:
    cfg_data = {
        "strategy": {"type": "noop"},
        "backtest": {"fill_model": "next_open", "commission_per_contract_usd": 0.5},
        "trading": {"enabled": False, "allow_live": False},
        "instrument": {
            "symbol": "NQ",
            "exchange": "CME",
            "currency": "USD",
            "multiplier": 20.0,
            "strategy": {"type": "custom_spec", "custom_spec": {"qty": 2}},
            "backtest": {"fill_model": "close", "commission_per_contract_usd": 3.0},
            "trading": {"enabled": True, "allow_live": True},
        },
    }

    with caplog.at_level(logging.WARNING, logger="trader.config"):
        cfg = AppConfig.model_validate(cfg_data)

    assert cfg.strategy.type == "noop"
    assert cfg.backtest.commission_per_contract_usd == 0.5
    assert cfg.trading.enabled is False
    assert cfg.instrument.multiplier == 20.0
    assert any("Ignoring instrument." in message for message in caplog.messages)
