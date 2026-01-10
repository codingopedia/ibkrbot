from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    instance_id: str = "bot1"


class OrderConfig(BaseModel):
    default_tif: str = "DAY"
    outside_rth: bool = False


class TradingConfig(BaseModel):
    enabled: bool = False
    allow_live: bool = False


class DataConfig(BaseModel):
    bars_enabled: bool = False
    bar_size: str = "1 min"
    duration: str = "1 D"
    use_rth: bool = False


class CustomSpecStrategyConfig(BaseModel):
    qty: int = 1
    cooldown_seconds: int = 300
    min_warmup_bars: int = 50
    long_only: bool = True
    trade_on_live_only: bool = True
    rules: dict[str, Any] = Field(default_factory=dict)


class TimeWindowConfig(BaseModel):
    start: str
    end: str


class BreakEvenConfig(BaseModel):
    enabled: bool = False
    trigger_r: float = 1.0
    offset_ticks: float = 0.0


class ShadowVariantsConfig(BaseModel):
    enabled: bool = True
    variant_b_tp_r: float = 2.0
    variant_c_sl_tight_r: float = 0.5


class StrategyORBVariantAConfig(BaseModel):
    timezone: str = "UTC"
    allow_short: bool = False
    max_trades_per_day: int = 1
    min_tick: float = 0.1
    range_window: TimeWindowConfig = Field(default_factory=lambda: TimeWindowConfig(start="00:00", end="05:00"))
    entry_window: TimeWindowConfig = Field(default_factory=lambda: TimeWindowConfig(start="07:00", end="10:00"))
    flat_time: str = "20:55"
    breakout_buffer_ticks: float = 1.0
    sl_buffer_ticks: float = 1.0
    tp_r: float = 1.5
    be: BreakEvenConfig = Field(default_factory=BreakEvenConfig)
    shadow_variants: ShadowVariantsConfig = Field(default_factory=ShadowVariantsConfig)
    qty: int = 1


class StrategyConfig(BaseModel):
    type: Literal["noop", "ema_cross", "custom_spec", "orb_variant_a"] = "noop"
    custom_spec: CustomSpecStrategyConfig = Field(default_factory=CustomSpecStrategyConfig)
    orb_variant_a: StrategyORBVariantAConfig = Field(default_factory=StrategyORBVariantAConfig)


class BacktestConfig(BaseModel):
    fill_model: Literal["next_open", "close"] = "next_open"
    commission_per_contract_usd: float = 1.0
    slippage_ticks: int = 0
    min_tick: float = 0.1


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
    connect_timeout_seconds: int = 20


class BrokerConfig(BaseModel):
    type: Literal["sim", "ibkr"] = "sim"
    ibkr: IBKRConfig = Field(default_factory=IBKRConfig)


class RiskConfig(BaseModel):
    max_position: int = 1
    max_daily_loss_usd: float = 100.0
    max_order_size: int = 1


class ReconcileConfig(BaseModel):
    unknown_orders_policy: Literal["HALT", "IGNORE", "CANCEL"] = "HALT"


class AppConfig(BaseModel):
    env: Env = "paper"
    log: LogConfig = Field(default_factory=LogConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    orders: OrderConfig = Field(default_factory=OrderConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    instrument: InstrumentConfig = Field(default_factory=InstrumentConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    reconcile: ReconcileConfig = Field(default_factory=ReconcileConfig)

    @model_validator(mode="before")
    @classmethod
    def _promote_instrument_configs(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        data = dict(data)
        instrument_cfg = data.get("instrument")
        if not isinstance(instrument_cfg, dict):
            return data

        instrument_data = dict(instrument_cfg)
        promoted_keys: list[str] = []
        conflict_keys: list[str] = []

        for key in ("strategy", "backtest", "trading"):
            if key in instrument_data:
                if key in data:
                    conflict_keys.append(key)
                else:
                    data[key] = instrument_data[key]
                    promoted_keys.append(key)
                instrument_data.pop(key, None)

        logger = logging.getLogger(__name__)
        if promoted_keys:
            logger.warning("Promoted instrument.%s to top-level config", ", ".join(promoted_keys))
        if conflict_keys:
            logger.warning(
                "Ignoring instrument.%s because top-level config is set", ", ".join(conflict_keys)
            )

        data["instrument"] = instrument_data
        return data


def load_config(path: str | Path) -> AppConfig:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return AppConfig.model_validate(data)
