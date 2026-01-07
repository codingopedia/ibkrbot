from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict

from pythonjsonlogger import jsonlogger

from trader.config import LogConfig


def setup_logging(cfg: LogConfig) -> None:
    level = getattr(logging, cfg.level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    # Avoid duplicate handlers on re-run in notebooks etc.
    for h in list(root.handlers):
        root.removeHandler(h)

    Path(cfg.file).parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(cfg.file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setLevel(level)

    if cfg.json:
        formatter = jsonlogger.JsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
        )
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    handler.setFormatter(formatter)
    root.addHandler(handler)

    # Also log to console in dev
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)
