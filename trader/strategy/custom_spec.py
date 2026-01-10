from __future__ import annotations

import logging
from typing import Optional

from trader.config import CustomSpecStrategyConfig
from trader.events import BarEvent, MarketEvent, Signal
from trader.strategy.base import Strategy


class CustomSpecStrategy(Strategy):
    """Placeholder strategy driven by external spec (rules to be provided).

    Currently only warms up on bars and emits no signals.
    """

    _LOG_EVERY_BARS = 10

    def __init__(self, cfg: CustomSpecStrategyConfig, logger: Optional[logging.Logger] = None) -> None:
        self.cfg = cfg
        self._log = logger or logging.getLogger("strategy.custom_spec")
        self.bars_seen = 0

    @property
    def warmup_done(self) -> bool:
        return self.bars_seen >= self.cfg.min_warmup_bars

    def on_market(self, event: MarketEvent) -> Optional[Signal]:
        return None

    def on_bar(self, bar: BarEvent, position_qty: int) -> Optional[Signal]:
        self.bars_seen += 1
        warmup_done = self.warmup_done

        if (
            self.bars_seen == 1
            or self.bars_seen == self.cfg.min_warmup_bars
            or self.bars_seen % self._LOG_EVERY_BARS == 0
        ):
            self._log.info(
                "custom_spec_loaded",
                extra={
                    "bars_seen": self.bars_seen,
                    "warmup_done": warmup_done,
                    "min_warmup_bars": self.cfg.min_warmup_bars,
                    "qty": self.cfg.qty,
                    "cooldown_seconds": self.cfg.cooldown_seconds,
                    "long_only": self.cfg.long_only,
                    "trade_on_live_only": self.cfg.trade_on_live_only,
                    "position_qty": position_qty,
                },
            )

        # TODO: implement entry/exit rules once provided
        return None
