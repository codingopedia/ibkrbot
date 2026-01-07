import yaml
from typer.testing import CliRunner

from trader.main import app


runner = CliRunner()


def test_flatten_cli_sim(tmp_path):
    cfg_path = tmp_path / "cfg.yaml"
    cfg = {
        "env": "paper",
        "log": {"level": "INFO", "json": False, "file": str(tmp_path / "trader.log")},
        "storage": {"sqlite_path": str(tmp_path / "db.sqlite")},
        "runtime": {"heartbeat_seconds": 1},
        "broker": {"type": "sim"},
        "instrument": {"symbol": "MGC", "exchange": "COMEX", "currency": "USD", "multiplier": 10.0},
        "risk": {"max_position": 10, "max_daily_loss_usd": 1000.0, "max_order_size": 10},
    }
    cfg_path.write_text(yaml.safe_dump(cfg))

    result = runner.invoke(app, ["flatten", "-c", str(cfg_path), "--confirm"])
    assert result.exit_code == 0, result.output
    assert "Already flat" in result.output or "submitted flatten order" in result.output
