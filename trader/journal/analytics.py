from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Iterable, List, Optional
from zoneinfo import ZoneInfo

from trader.events import BarEvent


@dataclass
class ShadowExitResult:
    trade_id: str
    variant_name: str
    exit_ts: datetime
    exit_price: float
    pnl_usd: float
    reason_exit: str


def _normalize_ts(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _parse_flat_time(flat_time: Optional[str]) -> Optional[time]:
    if not flat_time:
        return None
    hour, minute = (int(part) for part in flat_time.split(":"))
    return time(hour=hour, minute=minute)


def simulate_exit_from_bars(
    *,
    bars: Iterable[BarEvent],
    side: str,
    entry_price: float,
    stop_price: float,
    tp_price: float,
    flat_time: Optional[str],
    tz_name: str,
) -> tuple[datetime, float, str]:
    tz = ZoneInfo(tz_name)
    flat_time_obj = _parse_flat_time(flat_time)

    bars_sorted = sorted(bars, key=lambda b: b.ts_utc)
    reason = "hold"
    exit_price = tp_price if side == "BUY" else tp_price
    exit_ts = bars_sorted[-1].ts_utc if bars_sorted else datetime.utcnow()

    for bar in bars_sorted:
        ts_local = _normalize_ts(bar.ts_utc).astimezone(tz)

        stop_hit = False
        tp_hit = False
        if side == "BUY":
            if bar.low <= stop_price:
                stop_hit = True
                exit_price = stop_price
            elif bar.high >= tp_price:
                tp_hit = True
                exit_price = tp_price
        else:
            if bar.high >= stop_price:
                stop_hit = True
                exit_price = stop_price
            elif bar.low <= tp_price:
                tp_hit = True
                exit_price = tp_price

        if stop_hit:
            reason = "stop"
            exit_ts = bar.ts_utc
            break
        if tp_hit:
            reason = "tp"
            exit_ts = bar.ts_utc
            break

        if flat_time_obj and ts_local.timetz().replace(tzinfo=None) >= flat_time_obj:
            reason = "flat"
            exit_price = bar.close
            exit_ts = bar.ts_utc
            break

    return exit_ts, exit_price, reason


def compute_mae_mfe(
    bars: Iterable[BarEvent], side: str, entry_price: float, exit_price: float, risk_per_unit: Optional[float]
) -> tuple[float, float, Optional[float]]:
    mae = 0.0
    mfe = 0.0
    for bar in bars:
        if side == "BUY":
            mae = max(mae, max(0.0, entry_price - bar.low))
            mfe = max(mfe, max(0.0, bar.high - entry_price))
        else:
            mae = max(mae, max(0.0, bar.high - entry_price))
            mfe = max(mfe, max(0.0, entry_price - bar.low))
    r_multiple = None
    if risk_per_unit and risk_per_unit > 0:
        pnl_per_unit = exit_price - entry_price if side == "BUY" else entry_price - exit_price
        r_multiple = pnl_per_unit / risk_per_unit
    return mae, mfe, r_multiple


def compute_shadow_exits(
    *,
    trade_id: str,
    bars: Iterable[BarEvent],
    side: str,
    entry_price: float,
    qty: int,
    price_multiplier: float,
    risk_per_unit: float,
    stop_price: float,
    tp_price: float,
    flat_time: Optional[str],
    tz_name: str,
    variant_b_tp_r: float,
    variant_c_sl_tight_r: float,
) -> List[ShadowExitResult]:
    bar_list = list(sorted(bars, key=lambda b: b.ts_utc))
    if not bar_list:
        return []
    results: List[ShadowExitResult] = []

    # Variant B: no BE, bigger TP
    tp_b = entry_price + variant_b_tp_r * risk_per_unit if side == "BUY" else entry_price - variant_b_tp_r * risk_per_unit
    exit_ts, exit_price, reason = simulate_exit_from_bars(
        bars=bar_list, side=side, entry_price=entry_price, stop_price=stop_price, tp_price=tp_b, flat_time=flat_time, tz_name=tz_name
    )
    pnl = (exit_price - entry_price) * qty * price_multiplier if side == "BUY" else (entry_price - exit_price) * qty * price_multiplier
    results.append(ShadowExitResult(trade_id=trade_id, variant_name="B_no_BE_bigger_TP", exit_ts=exit_ts, exit_price=exit_price, pnl_usd=pnl, reason_exit=reason))

    # Variant C: tighter SL
    if variant_c_sl_tight_r > 0:
        tight_stop = entry_price - variant_c_sl_tight_r * risk_per_unit if side == "BUY" else entry_price + variant_c_sl_tight_r * risk_per_unit
    else:
        tight_stop = stop_price
    exit_ts_c, exit_price_c, reason_c = simulate_exit_from_bars(
        bars=bar_list, side=side, entry_price=entry_price, stop_price=tight_stop, tp_price=tp_price, flat_time=flat_time, tz_name=tz_name
    )
    pnl_c = (exit_price_c - entry_price) * qty * price_multiplier if side == "BUY" else (entry_price - exit_price_c) * qty * price_multiplier
    results.append(
        ShadowExitResult(
            trade_id=trade_id,
            variant_name="C_no_BE_tighter_SL",
            exit_ts=exit_ts_c,
            exit_price=exit_price_c,
            pnl_usd=pnl_c,
            reason_exit=reason_c,
        )
    )

    return results
