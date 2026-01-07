from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field


Env = Literal["paper", "live"]


class LogConfig(BaseModel):
    level: str = "INFO"
    json_logs: bool = Field(default=True, alias="json")
    file: str = "run/trader.log"

    model_config = ConfigDict(populate_by_name=True)


class StorageConfig(BaseModel):
    sqlite_path: str = "run/trader.sqlite"


class RuntimeConfig(BaseModel):
    heartbeat_seconds: float = 2.0


class InstrumentConfig(BaseModel):
    symbol: str = "MGC"
    exchange: str = "COMEX"
    currency: str = "USD"
    multiplier: float = 10.0  # MGC=10, GC=100 (USD per 1.0 price move per contract)
    expiry: Optional[str] = None  # YYYYMM or YYYYMMDD (optional, IBKR adapter can auto-select)


class IBKRConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 1
    market_data_type: int = 3  # 3 = delayed


class BrokerConfig(BaseModel):
    type: Literal["sim", "ibkr"] = "sim"
    ibkr: IBKRConfig = Field(default_factory=IBKRConfig)


class RiskConfig(BaseModel):
    max_position: int = 1
    max_daily_loss_usd: float = 100.0
    max_order_size: int = 1


class AppConfig(BaseModel):
    env: Env = "paper"
    log: LogConfig = Field(default_factory=LogConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    instrument: InstrumentConfig = Field(default_factory=InstrumentConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)


def load_config(path: str | Path) -> AppConfig:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return AppConfig.model_validate(data)
