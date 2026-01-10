from __future__ import annotations

import csv
import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import typer

from trader.broker.base import Broker
from trader.bus import EventBus
from trader.config import AppConfig, load_config
from trader.events import BarEvent, Fill, MarketEvent, OrderIntent, OrderStatusUpdate, Signal
from trader.journal.daily import StrategyDailyAggregator
from trader.journal.trade_tracker import TradeTracker
from trader.ledger import Ledger
from trader.logging_setup import setup_logging
from trader.market_data import is_valid_price, is_valid_tick
from trader.persistence import Database
from trader.risk import RiskEngine
from trader.strategy import CustomSpecStrategy, NoopStrategy, ORBVariantAStrategy, Strategy
from trader.broker.ibkr.adapter import IBKRBroker
from trader.broker.sim import SimBroker
from trader.reconcile import reconcile_ibkr


app = typer.Typer(add_completion=False)


def _make_client_order_id() -> str:
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:10]


def _make_broker(cfg: AppConfig) -> Broker:
    if cfg.broker.type == "sim":
        return SimBroker()
    if cfg.broker.type == "ibkr":
        ibkr_cfg = cfg.broker.ibkr
        return IBKRBroker(
            host=ibkr_cfg.host,
            port=ibkr_cfg.port,
            client_id=ibkr_cfg.client_id,
            market_data_type=ibkr_cfg.market_data_type,
            connect_timeout_seconds=ibkr_cfg.connect_timeout_seconds,
            instrument=cfg.instrument,
            orders=cfg.orders,
            data=cfg.data,
            instance_id=cfg.runtime.instance_id,
        )
    raise ValueError(f"Unsupported broker type: {cfg.broker.type}")


def _make_strategy(cfg: AppConfig, log: logging.Logger) -> Strategy:
    strat_type = cfg.strategy.type
    if strat_type == "custom_spec":
        return CustomSpecStrategy(cfg.strategy.custom_spec, logger=log.getChild("custom_spec"))
    if strat_type == "noop":
        return NoopStrategy()
    if strat_type == "orb_variant_a":
        return ORBVariantAStrategy(cfg.strategy.orb_variant_a, cfg.instrument.symbol, logger=log.getChild("orb_variant_a"))
    log.warning("strategy_not_implemented", extra={"strategy_type": strat_type})
    return NoopStrategy()


def _run_impl(cfg: AppConfig, iterations: int) -> None:
    log = logging.getLogger("runner")
    log.info("starting", extra={"env": cfg.env, "symbol": cfg.instrument.symbol})

    trading_cfg = cfg.trading
    trading_enabled = bool(trading_cfg.enabled)
    if not trading_enabled:
        log.warning("TRADING DISABLED (read-only mode)")
    else:
        if cfg.env != "paper":
            if not trading_cfg.allow_live:
                msg = "Trading enabled but allow_live=false in non-paper env; refusing to start."
                log.error("live_trading_blocked", extra={"env": cfg.env, "allow_live": trading_cfg.allow_live})
                typer.echo(msg)
                raise typer.Exit(code=1)
            log.critical("LIVE TRADING ENABLED", extra={"env": cfg.env})
        else:
            log.info("trading_enabled", extra={"env": cfg.env})

    db = Database(cfg.storage.sqlite_path)
    ledger = Ledger(price_multiplier=cfg.instrument.multiplier)
    risk = RiskEngine(cfg.risk)
    strat: Strategy = _make_strategy(cfg, logging.getLogger("strategy"))
    orb_cfg = getattr(cfg.strategy, "orb_variant_a", None)
    daily_agg = StrategyDailyAggregator(
        db,
        symbol=cfg.instrument.symbol,
        strategy=cfg.strategy.type,
        timezone=getattr(orb_cfg, "timezone", "UTC"),
        range_start=getattr(getattr(orb_cfg, "range_window", None), "start", None),
        range_end=getattr(getattr(orb_cfg, "range_window", None), "end", None),
        entry_start=getattr(getattr(orb_cfg, "entry_window", None), "start", None),
        entry_end=getattr(getattr(orb_cfg, "entry_window", None), "end", None),
    )
    tracker = TradeTracker(
        db,
        instance_id=cfg.runtime.instance_id,
        strategy_name=cfg.strategy.type,
        symbol=cfg.instrument.symbol,
        price_multiplier=cfg.instrument.multiplier,
        tz_name=getattr(orb_cfg, "timezone", "UTC") if orb_cfg else "UTC",
        shadow_cfg=orb_cfg.shadow_variants if orb_cfg else None,
        logger=logging.getLogger("trade_tracker"),
        on_trade_closed=daily_agg.bump_trade_closed,
    )
    pending_entry_context: dict[str, Any] = {}

    broker = _make_broker(cfg)
    if cfg.data.bars_enabled:
        log.info(
            "bars_enabled=true",
            extra={"bar_size": cfg.data.bar_size, "duration": cfg.data.duration, "use_rth": cfg.data.use_rth},
        )

    last_market: Optional[MarketEvent] = None
    last_bar: Optional[BarEvent] = None
    bar_counter = 0
    snapshot_count = 0
    snapshot_logged = False

    def handle_signal(sig: Optional[Signal]) -> None:
        if sig is None:
            return
        if not trading_enabled:
            log.info(
                "trading_disabled_skip_order",
                extra={"symbol": sig.symbol, "side": sig.side, "qty": sig.qty, "env": cfg.env},
            )
            return

        intent = OrderIntent(
            ts=datetime.utcnow(),
            symbol=sig.symbol,
            side=sig.side,
            qty=sig.qty,
            order_type="MKT",
            client_order_id=_make_client_order_id(),
        )

        # Risk check vs current position
        pos_qty = ledger.positions.get(intent.symbol).qty if intent.symbol in ledger.positions else 0
        risk.evaluate_order(intent, pos_qty)

        db.insert_order(intent, status="Created")
        ack = broker.place_order(intent)
        db.update_order_ack(intent.client_order_id, ack.broker_order_id, ack.status or "Submitted")
        log.info("order_submitted", extra={"client_order_id": intent.client_order_id, "broker_order_id": ack.broker_order_id})

    def on_market(evt: MarketEvent) -> None:
        nonlocal last_market
        last_market = evt
        log.info("market", extra={"symbol": evt.symbol, "bid": evt.bid, "ask": evt.ask, "last": evt.last})

        sig = strat.on_market(evt)
        handle_signal(sig)

    def on_bar(evt: BarEvent) -> None:
        nonlocal last_bar, pending_entry_context, bar_counter, snapshot_count, snapshot_logged
        last_bar = evt
        db.upsert_bar(evt)

        if evt.is_snapshot:
            snapshot_count += 1
        else:
            if snapshot_count > 0 and not snapshot_logged:
                log.info("bars_snapshot_loaded", extra={"symbol": evt.symbol, "count": snapshot_count})
                snapshot_logged = True
            bar_counter += 1

        # Custom strategy hook on bars (dry-run ready; no orders if trading disabled)
        bar_sig: Optional[Signal] = None
        ctx: dict[str, Any] = {}
        if hasattr(strat, "on_bar"):
            pos_qty = ledger.positions.get(evt.symbol).qty if evt.symbol in ledger.positions else 0
            try:
                bar_sig = strat.on_bar(evt, pos_qty)  # type: ignore[attr-defined]
                ctx = getattr(strat, "get_signal_context", lambda: {})() or {}
            except Exception as exc:  # pragma: no cover - strategy errors should not crash loop
                log.error("strategy_on_bar_failed", extra={"error": str(exc)})
                ctx = {}

        # Persist daily state snapshots (range, counters) even when no signal is produced.
        daily_state = getattr(strat, "get_daily_state", lambda: None)()
        if daily_state:
            daily_agg.update_state(**daily_state)
        completed_days = getattr(strat, "consume_completed_days", lambda: [])()
        for day_state in completed_days:
            daily_agg.update_state(**day_state)

        if bar_sig:
            sig_type = ctx.get("type", "signal")
            if sig_type == "entry" and "trade_id" not in ctx:
                ctx = {**ctx, "trade_id": f"trade-{uuid.uuid4().hex[:8]}"}
            db.insert_signal(
                ts=bar_sig.ts,
                symbol=bar_sig.symbol,
                strategy=cfg.strategy.type,
                type=sig_type,
                side=bar_sig.side,
                qty=bar_sig.qty,
                reason=bar_sig.reason,
                price_ref=ctx.get("price_ref", evt.close),
                bar_ts=evt.ts_utc,
                is_snapshot=evt.is_snapshot,
                extras=ctx,
            )
            if sig_type == "entry":
                pending_entry_context = ctx
            elif sig_type == "exit":
                tracker.set_exit_reason(bar_sig.reason)
        handle_signal(bar_sig)

        if not evt.is_snapshot and (bar_counter == 1 or bar_counter % 20 == 0):
            log.info(
                "bar_received",
                extra={
                    "symbol": evt.symbol,
                    "ts": evt.ts_utc.isoformat(),
                    "open": evt.open,
                    "high": evt.high,
                    "low": evt.low,
                    "close": evt.close,
                    "volume": evt.volume,
                    "bar_counter": bar_counter,
                },
            )

    def on_fill(fill: Fill) -> None:
        nonlocal pending_entry_context
        db.update_order_status(fill.client_order_id, "Filled", fill.broker_order_id)
        log.info(
            "fill",
            extra={
                "client_order_id": fill.client_order_id,
                "broker_order_id": fill.broker_order_id,
                "symbol": fill.symbol,
                "side": fill.side,
                "qty": fill.qty,
                "price": fill.price,
                "commission": fill.commission,
            },
        )
        prev_qty = ledger.positions.get(fill.symbol).qty if fill.symbol in ledger.positions else 0
        ledger.on_fill(fill)
        db.insert_fill(fill)
        new_qty = ledger.positions.get(fill.symbol).qty
        tracker.set_pending_context(pending_entry_context)
        tracker.on_fill(fill, prev_qty, new_qty)
        if new_qty == 0:
            pending_entry_context = {}

    def on_order_status(update: OrderStatusUpdate) -> None:
        db.update_order_status(update.client_order_id, update.status, update.broker_order_id)

    def on_commission(exec_id: str, commission: float) -> None:
        updated = db.update_fill_commission(exec_id, commission)
        if updated:
            log.info("commission_backfilled", extra={"exec_id": exec_id, "commission": commission})

    try:
        broker.connect()

        if isinstance(broker, IBKRBroker):
            reconcile_ibkr(
                broker,
                db,
                ledger,
                risk,
                cfg.instrument.symbol,
                instance_id=cfg.runtime.instance_id,
                unknown_orders_policy=cfg.reconcile.unknown_orders_policy,
                env=cfg.env,
            )
            if risk.halted:
                log.error("halted_after_reconcile")
                return

        if cfg.data.bars_enabled:
            log.info(
                "subscribing_bars",
                extra={
                    "symbol": cfg.instrument.symbol,
                    "bar_size": cfg.data.bar_size,
                    "duration": cfg.data.duration,
                    "use_rth": cfg.data.use_rth,
                },
            )
            try:
                broker.subscribe_bars(cfg.instrument.symbol, on_bar)
            except Exception as exc:
                log.error("bars_subscribe_failed", extra={"error": str(exc)})

        # MVP: a tiny loop that:
        # - emits a few market events
        # - polls fills
        # - writes pnl snapshots
        i = 0
        while True:
            broker.subscribe_market_data(cfg.instrument.symbol, on_market)
            if cfg.data.bars_enabled:
                broker.poll_bars()
            broker.poll_order_status(on_order_status)
            broker.poll_fills(on_fill)
            broker.poll_commissions(on_commission)

            last_price = None
            if last_market and is_valid_price(last_market.last):
                last_price = last_market.last
            elif last_bar:
                last_price = last_bar.close
            snap = ledger.snapshot(cfg.instrument.symbol, last_price)
            db.insert_pnl_snapshot(snap)

            # risk guard
            risk.evaluate_pnl(snap)
            if risk.halted:
                log.error("halted_by_risk")
                break

            time.sleep(cfg.runtime.heartbeat_seconds)

            i += 1
            if iterations > 0 and i >= iterations:
                log.info("stopping_after_iterations", extra={"iterations": iterations})
                break

    except KeyboardInterrupt:
        log.info("stopping_keyboard_interrupt")
    finally:
        if snapshot_count > 0 and not snapshot_logged:
            log.info("bars_snapshot_loaded", extra={"symbol": cfg.instrument.symbol, "count": snapshot_count})
        broker.disconnect()
        db.close()
        log.info("stopped")


@app.command()
def run(config: str = typer.Option(..., "--config", "-c"), iterations: int = typer.Option(0, "--iterations", "-n", help="0 = run forever")) -> None:
    """Run the bot loop (dry wiring by default)."""
    cfg: AppConfig = load_config(config)
    setup_logging(cfg.log)
    _run_impl(cfg, iterations)


def _export_table(conn: sqlite3.Connection, path: Path, columns: list[str], query: str, params: list[Any]) -> None:
    rows = conn.execute(query, params).fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for row in rows:
            values = []
            for col in columns:
                try:
                    values.append(row[col])
                except Exception:
                    values.append(None)
            writer.writerow(values)


def _export_tables(
    cfg: AppConfig, outdir: Path, days: int, symbol_override: Optional[str] = None, session_id: Optional[str] = None
) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.storage.sqlite_path)
    conn.row_factory = sqlite3.Row
    ts_tag = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    symbol = symbol_override or cfg.instrument.symbol
    base = session_id or f"{ts_tag}_{symbol}_{cfg.strategy.type}"
    paths: list[Path] = []

    cutoff_ts = datetime.utcnow() - timedelta(days=days) if days > 0 else None
    cutoff_iso = cutoff_ts.isoformat() if cutoff_ts else None
    cutoff_day = cutoff_ts.date().isoformat() if cutoff_ts else None

    bar_query = "SELECT ts, symbol, open, high, low, close, volume FROM bars_1m WHERE symbol=?"
    bar_params: list[Any] = [symbol]
    if cutoff_iso:
        bar_query += " AND ts >= ?"
        bar_params.append(cutoff_iso)
    bar_query += " ORDER BY ts"
    path = outdir / f"{base}_bars_1m.csv"
    _export_table(conn, path, ["ts", "symbol", "open", "high", "low", "close", "volume"], bar_query, bar_params)
    paths.append(path)

    sig_query = "SELECT ts, symbol, strategy, type, side, qty, reason, price_ref, bar_ts, is_snapshot, extras_json FROM strategy_signals WHERE symbol=? AND strategy=?"
    sig_params: list[Any] = [symbol, cfg.strategy.type]
    if cutoff_iso:
        sig_query += " AND ts >= ?"
        sig_params.append(cutoff_iso)
    sig_query += " ORDER BY ts"
    path = outdir / f"{base}_strategy_signals.csv"
    _export_table(
        conn,
        path,
        ["ts", "symbol", "strategy", "type", "side", "qty", "reason", "price_ref", "bar_ts", "is_snapshot", "extras_json"],
        sig_query,
        sig_params,
    )
    paths.append(path)

    tj_query = "SELECT trade_id, instance_id, strategy, symbol, entry_ts, entry_price, entry_side, entry_reason, exit_ts, exit_price, exit_reason, qty, pnl_usd, range_high, range_low, risk_per_unit FROM trade_journal WHERE symbol=? AND strategy=?"
    tj_params: list[Any] = [symbol, cfg.strategy.type]
    if cutoff_iso:
        tj_query += " AND (exit_ts IS NULL OR exit_ts >= ?)"
        tj_params.append(cutoff_iso)
    path = outdir / f"{base}_trade_journal.csv"
    _export_table(
        conn,
        path,
        [
            "trade_id",
            "instance_id",
            "strategy",
            "symbol",
            "entry_ts",
            "entry_price",
            "entry_side",
            "entry_reason",
            "exit_ts",
            "exit_price",
            "exit_reason",
            "qty",
            "pnl_usd",
            "range_high",
            "range_low",
            "risk_per_unit",
        ],
        tj_query,
        tj_params,
    )
    paths.append(path)

    tm_query = "SELECT trade_id, duration_seconds, mfe, mae, r_multiple FROM trade_metrics"
    tm_params: list[Any] = []
    path = outdir / f"{base}_trade_metrics.csv"
    _export_table(conn, path, ["trade_id", "duration_seconds", "mfe", "mae", "r_multiple"], tm_query, tm_params)
    paths.append(path)

    ts_query = "SELECT trade_id, variant_name, exit_ts, exit_price, pnl_usd, reason_exit FROM trade_shadow"
    ts_params: list[Any] = []
    if cutoff_iso:
        ts_query += " WHERE exit_ts IS NULL OR exit_ts >= ?"
        ts_params.append(cutoff_iso)
    path = outdir / f"{base}_trade_shadow.csv"
    _export_table(
        conn,
        path,
        ["trade_id", "variant_name", "exit_ts", "exit_price", "pnl_usd", "reason_exit"],
        ts_query,
        ts_params,
    )
    paths.append(path)

    sd_query = "SELECT day, symbol, strategy, timezone, range_start, range_end, entry_start, entry_end, range_high, range_low, range_bars, signals_count, entries_count, exits_count, trades_closed_count, notes_json FROM strategy_daily WHERE symbol=? AND strategy=?"
    sd_params: list[Any] = [symbol, cfg.strategy.type]
    if cutoff_day:
        sd_query += " AND day >= ?"
        sd_params.append(cutoff_day)
    path = outdir / f"{base}_strategy_daily.csv"
    _export_table(
        conn,
        path,
        [
            "day",
            "symbol",
            "strategy",
            "timezone",
            "range_start",
            "range_end",
            "entry_start",
            "entry_end",
            "range_high",
            "range_low",
            "range_bars",
            "signals_count",
            "entries_count",
            "exits_count",
            "trades_closed_count",
            "notes_json",
        ],
        sd_query,
        sd_params,
    )
    paths.append(path)
    return paths


def _generate_report(
    cfg: AppConfig, days: int, outdir: Path, symbol_override: Optional[str] = None, session_id: Optional[str] = None
) -> tuple[Path, dict[str, Any]]:
    outdir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.storage.sqlite_path)
    conn.row_factory = sqlite3.Row
    cutoff_ts = datetime.utcnow() - timedelta(days=days) if days > 0 else None
    cutoff_iso = cutoff_ts.isoformat() if cutoff_ts else None
    cutoff_day = cutoff_ts.date().isoformat() if cutoff_ts else None
    symbol = symbol_override or cfg.instrument.symbol

    signals_params: list[Any] = [symbol, cfg.strategy.type]
    signals_query = "SELECT COUNT(*) AS c FROM strategy_signals WHERE symbol=? AND strategy=?"
    if cutoff_iso:
        signals_query += " AND ts >= ?"
        signals_params.append(cutoff_iso)
    signals_count = int(conn.execute(signals_query, signals_params).fetchone()["c"])

    trade_params: list[Any] = [symbol, cfg.strategy.type]
    trade_query = "SELECT pnl_usd FROM trade_journal WHERE symbol=? AND strategy=? AND exit_ts IS NOT NULL"
    if cutoff_iso:
        trade_query += " AND exit_ts >= ?"
        trade_params.append(cutoff_iso)
    pnl_rows = [row["pnl_usd"] for row in conn.execute(trade_query, trade_params)]
    pnl_values = [float(p) for p in pnl_rows if p is not None]
    closed_trades = len(pnl_values)
    total_pnl = sum(pnl_values) if pnl_values else 0.0
    wins = [p for p in pnl_values if p > 0]
    losses = [p for p in pnl_values if p < 0]
    winrate = (len(wins) / closed_trades) if closed_trades else None
    avg_pnl = (total_pnl / closed_trades) if closed_trades else None
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else None

    shadow_params: list[Any] = []
    shadow_query = "SELECT variant_name, AVG(pnl_usd) AS avg_pnl FROM trade_shadow"
    if cutoff_iso:
        shadow_query += " WHERE exit_ts IS NULL OR exit_ts >= ?"
        shadow_params.append(cutoff_iso)
    shadow_query += " GROUP BY variant_name"
    shadow_rows = conn.execute(shadow_query, shadow_params).fetchall()
    shadow_avg = {row["variant_name"]: row["avg_pnl"] for row in shadow_rows}

    days_params: list[Any] = [symbol, cfg.strategy.type]
    days_query = "SELECT COUNT(DISTINCT day) AS d FROM strategy_daily WHERE symbol=? AND strategy=?"
    if cutoff_day:
        days_query += " AND day >= ?"
        days_params.append(cutoff_day)
    days_count = int(conn.execute(days_query, days_params).fetchone()["d"])

    summary = {
        "days_count": days_count,
        "signals_count": signals_count,
        "closed_trades": closed_trades,
        "total_pnl": total_pnl,
        "winrate": winrate,
        "avg_pnl": avg_pnl,
        "profit_factor": profit_factor,
        "shadow_avg": shadow_avg,
        "symbol": symbol,
    }

    ts_tag = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    fname = session_id or f"{ts_tag}_orb_report"
    report_path = outdir / f"{fname}.json"
    report_path.write_text(json.dumps(summary, indent=2, ensure_ascii=True), encoding="utf-8")
    return report_path, summary


_trial_runner = _run_impl


def _append_trial_log(message: str) -> None:
    path = Path("run/trial_build.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(message + "\n")


def _read_counts(db: Database, symbol: str, strategy: str) -> dict[str, int]:
    conn = db.conn

    def _count(query: str, params: list[Any]) -> int:
        row = conn.execute(query, params).fetchone()
        try:
            return int(row[0]) if row is not None else 0
        except Exception:
            return 0

    return {
        "bars": _count("SELECT COUNT(*) FROM bars_1m WHERE symbol=?", [symbol]),
        "signals": _count("SELECT COUNT(*) FROM strategy_signals WHERE symbol=? AND strategy=?", [symbol, strategy]),
        "daily": _count("SELECT COUNT(*) FROM strategy_daily WHERE symbol=? AND strategy=?", [symbol, strategy]),
        "trades": _count("SELECT COUNT(*) FROM trade_journal WHERE symbol=? AND strategy=?", [symbol, strategy]),
    }


def _collect_counts(sqlite_path: str, symbol: str, strategy: str) -> dict[str, int]:
    db = Database(sqlite_path)
    counts = _read_counts(db, symbol, strategy)
    db.close()
    return counts


@app.command()
def export(
    config: str = typer.Option(..., "--config", "-c"),
    outdir: str = typer.Option("run/exports", "--outdir"),
    days: int = typer.Option(14, "--days"),
    symbol: Optional[str] = typer.Option(None, "--symbol", help="Override symbol filter"),
) -> None:
    """Export key tables to CSV for offline analysis."""
    cfg: AppConfig = load_config(config)
    setup_logging(cfg.log)
    log = logging.getLogger("export")
    paths = _export_tables(cfg, Path(outdir), days, symbol_override=symbol)
    log.info("export_complete", extra={"outdir": str(outdir), "files": [str(p) for p in paths]})


@app.command()
def report(
    config: str = typer.Option(..., "--config", "-c"),
    days: int = typer.Option(14, "--days"),
    outdir: str = typer.Option("run/reports", "--outdir"),
    symbol: Optional[str] = typer.Option(None, "--symbol", help="Override symbol filter"),
) -> None:
    """Print quick performance summary and save JSON report."""
    cfg: AppConfig = load_config(config)
    setup_logging(cfg.log)
    log = logging.getLogger("report")
    report_path, summary = _generate_report(cfg, days, Path(outdir), symbol_override=symbol)

    typer.echo(f"days={summary['days_count']} signals={summary['signals_count']} closed_trades={summary['closed_trades']} total_pnl={summary['total_pnl']}")
    if summary["winrate"] is not None:
        typer.echo(f"winrate={summary['winrate']:.2%} avg_pnl={summary['avg_pnl']}")
    if summary["profit_factor"] is not None:
        typer.echo(f"profit_factor={summary['profit_factor']:.2f}")
    if summary["shadow_avg"]:
        typer.echo(f"shadow_avg={summary['shadow_avg']}")
    log.info("report_complete", extra={"path": str(report_path)})


@app.command()
def trial(
    config: str = typer.Option(..., "--config", "-c"),
    iterations: int = typer.Option(3600, "--iterations", "-n"),
    outdir: str = typer.Option("run/trials", "--outdir"),
    export_days: int = typer.Option(14, "--export-days"),
    symbol: Optional[str] = typer.Option(None, "--symbol", help="Override symbol for sanity checks/export/report"),
) -> None:
    """Automate a read-only ORB data trial: run -> report -> export."""
    cfg: AppConfig = load_config(config)
    if cfg.trading.enabled:
        msg = "trial is read-only; set trading.enabled=false"
        typer.echo(msg)
        _append_trial_log(msg)
        raise typer.Exit(code=1)

    setup_logging(cfg.log)
    log = logging.getLogger("trial")
    sym = symbol or cfg.instrument.symbol
    strategy = cfg.strategy.type
    session_id = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{sym}_{strategy}_{cfg.runtime.instance_id}"
    _append_trial_log(
        f"trial_start session_id={session_id} config={config} symbol={sym} iterations={iterations} outdir={outdir} export_days={export_days}"
    )

    before_counts = _collect_counts(cfg.storage.sqlite_path, sym, strategy)
    log.info("before_counts", extra=before_counts)
    _append_trial_log(f"before_counts {before_counts}")

    _trial_runner(cfg, iterations)

    after_counts = _collect_counts(cfg.storage.sqlite_path, sym, strategy)
    delta_counts = {k: after_counts.get(k, 0) - before_counts.get(k, 0) for k in after_counts}
    log.info("after_counts", extra={"after": after_counts, "delta": delta_counts})
    _append_trial_log(f"after_counts after={after_counts} delta={delta_counts}")

    if delta_counts.get("bars", 0) == 0:
        log.warning("No new bars collected (check bars_enabled/TWS/data)")
        _append_trial_log("WARNING: No new bars collected (check bars_enabled/TWS/data)")
    if delta_counts.get("daily", 0) == 0:
        log.warning("No daily summary updated yet (may require day rollover)")
        _append_trial_log("WARNING: No daily summary updated yet (may require day rollover)")
    if delta_counts.get("signals", 0) == 0:
        log.info("No signals during this window (not necessarily an error)")
        _append_trial_log("INFO: No signals during this window (not necessarily an error)")

    base_out = Path(outdir)
    report_dir = base_out / "reports"
    export_dir = base_out / "exports"
    report_path, summary = _generate_report(cfg, export_days, report_dir, symbol_override=sym, session_id=session_id)
    export_paths = _export_tables(cfg, export_dir, export_days, symbol_override=sym, session_id=session_id)

    _append_trial_log(f"report_path={report_path}")
    _append_trial_log(f"export_files={[str(p) for p in export_paths]}")

    typer.echo(f"session_id={session_id}")
    typer.echo(f"report={report_path}")
    typer.echo(f"exports_dir={export_dir}")
    typer.echo("trial complete")
    log.info("trial_complete", extra={"report": str(report_path), "exports": [str(p) for p in export_paths]})


@app.command()
def smoke(
    config: str = typer.Option(..., "--config", "-c"),
    qty: int = typer.Option(1, "--qty"),
    side: str = typer.Option("BUY", "--side"),
    timeout_seconds: int = typer.Option(30, "--timeout-seconds"),
    order_type: str = typer.Option("MKT", "--order-type"),
    limit_price: Optional[float] = typer.Option(None, "--limit-price"),
    no_wait: bool = typer.Option(False, "--no-wait", help="Do not wait for fill; exit after ACK"),
) -> None:
    """IBKR smoke test: connect, get a tick, send 1 MKT order, wait for fill."""
    cfg: AppConfig = load_config(config)
    setup_logging(cfg.log)
    log = logging.getLogger("smoke")
    log.warning("SMOKE TEST - PAPER/DEMO ONLY", extra={"env": cfg.env})

    if cfg.env != "paper":
        typer.echo("Smoke test allowed only in paper/demo env; refusing to run.")
        raise typer.Exit(code=1)
    if cfg.broker.type != "ibkr":
        typer.echo("Smoke test requires broker.type=ibkr in config.")
        raise typer.Exit(code=1)

    side_up = side.upper()
    if side_up not in ("BUY", "SELL"):
        typer.echo("Side must be BUY or SELL.")
        raise typer.Exit(code=1)

    order_type_up = order_type.upper()
    if order_type_up not in ("MKT", "LMT"):
        typer.echo("order-type must be MKT or LMT.")
        raise typer.Exit(code=1)
    if order_type_up == "LMT" and limit_price is None:
        typer.echo("limit-price is required for LMT orders.")
        raise typer.Exit(code=1)

    db = Database(cfg.storage.sqlite_path)
    ledger = Ledger(price_multiplier=cfg.instrument.multiplier)
    broker = _make_broker(cfg)

    last_tick: Optional[MarketEvent] = None
    valid_tick: Optional[MarketEvent] = None
    fills: list[Fill] = []

    def on_market(evt: MarketEvent) -> None:
        nonlocal last_tick, valid_tick
        last_tick = evt
        if valid_tick is None and is_valid_tick(evt):
            valid_tick = evt

    def on_fill(fill: Fill) -> None:
        fills.append(fill)
        ledger.on_fill(fill)
        db.insert_fill(fill)
        db.update_order_status(fill.client_order_id, "Filled", fill.broker_order_id)

    def on_order_status(update: OrderStatusUpdate) -> None:
        db.update_order_status(update.client_order_id, update.status, update.broker_order_id)

    def on_commission(exec_id: str, commission: float) -> None:
        db.update_fill_commission(exec_id, commission)

    try:
        broker.connect()
        broker.subscribe_market_data(cfg.instrument.symbol, on_market)

        tick_deadline = time.time() + 15
        while valid_tick is None and time.time() < tick_deadline:
            broker.poll_fills(on_fill)  # pump event loop for ib_insync
            time.sleep(0.1)
        if valid_tick is None:
            log.warning("No valid market data; proceeding anyway (paper only)")

        intent = OrderIntent(
            ts=datetime.utcnow(),
            symbol=cfg.instrument.symbol,
            side=side_up,
            qty=qty,
            order_type=order_type_up,
            limit_price=limit_price,
            client_order_id=_make_client_order_id(),
        )
        db.insert_order(intent, status="Created")
        ack = broker.place_order(intent)
        db.update_order_ack(intent.client_order_id, ack.broker_order_id, ack.status or "Submitted")
        log.info(
            "smoke_order_submitted",
            extra={"client_order_id": intent.client_order_id, "broker_order_id": ack.broker_order_id, "side": side_up, "qty": qty},
        )

        if no_wait:
            typer.echo(
                f"submitted order client_id={intent.client_order_id} broker_id={ack.broker_order_id} "
                f"type={order_type_up} limit_price={limit_price}"
            )
            return

        deadline = time.time() + timeout_seconds
        while not fills and time.time() < deadline:
            broker.poll_order_status(on_order_status)
            broker.poll_fills(on_fill)
            broker.poll_commissions(on_commission)
            time.sleep(0.1)
        if not fills:
            raise RuntimeError(f"No fill received within {timeout_seconds} seconds")

        fill = fills[-1]
        last_price = valid_tick.last if valid_tick else None
        snap = ledger.snapshot(cfg.instrument.symbol, last_price)
        db.insert_pnl_snapshot(snap)
        pos = ledger.positions.get(cfg.instrument.symbol)
        pos_qty = pos.qty if pos else 0
        pos_avg = pos.avg_price if pos else 0.0

        typer.echo(
            f"broker_order_id={fill.broker_order_id} price={fill.price} "
            f"position_qty={pos_qty} position_avg={pos_avg} "
            f"unrealized_usd={snap.unrealized_usd} realized_usd={snap.realized_usd}"
        )
    except Exception as exc:
        log.error("smoke_failed", extra={"error": str(exc)})
        typer.echo(f"Smoke test failed: {exc}")
        raise typer.Exit(code=1)
    finally:
        broker.disconnect()
        db.close()


@app.command()
def doctor(config: str = typer.Option(..., "--config", "-c")) -> None:
    """Read-only IBKR diagnostics: connectivity, contracts, market data."""
    cfg: AppConfig = load_config(config)
    setup_logging(cfg.log)
    log = logging.getLogger("doctor")
    log.warning("DOCTOR - READ ONLY", extra={"env": cfg.env})

    if cfg.broker.type != "ibkr":
        typer.echo("Doctor command requires broker.type=ibkr in config.")
        raise typer.Exit(code=1)

    broker = _make_broker(cfg)

    try:
        broker.connect()
        ib = broker.raw_ib()  # type: ignore[attr-defined]

        server_version = ib.client.serverVersion()
        accounts_attr = getattr(ib, "managedAccounts", [])
        accounts_raw = accounts_attr() if callable(accounts_attr) else accounts_attr
        accounts_list = accounts_raw.split(",") if isinstance(accounts_raw, str) else list(accounts_raw or [])
        now_ib = ib.reqCurrentTime()

        typer.echo(f"serverVersion={server_version}")
        typer.echo(f"managedAccounts={accounts_list}")
        typer.echo(f"currentTime={now_ib}")
        typer.echo(f"market_data_type={cfg.broker.ibkr.market_data_type}")

        contracts = broker.contract_candidates(limit=5)  # type: ignore[attr-defined]
        typer.echo("nearest_contracts:")
        for c in contracts:
            typer.echo(f"  {c.lastTradeDateOrContractMonth} conId={c.conId}")

        first_tick: Optional[MarketEvent] = None
        valid_tick: Optional[MarketEvent] = None

        def on_market(evt: MarketEvent) -> None:
            nonlocal first_tick, valid_tick
            if first_tick is None:
                first_tick = evt
            if valid_tick is None and is_valid_tick(evt):
                valid_tick = evt

        broker.subscribe_market_data(cfg.instrument.symbol, on_market)
        deadline = time.time() + 10
        while valid_tick is None and time.time() < deadline:
            broker.poll_fills(lambda _fill: None)
            time.sleep(0.1)

        if first_tick:
            typer.echo(
                f"tick: bid={first_tick.bid} ask={first_tick.ask} last={first_tick.last} ts={first_tick.ts}"
            )
        else:
            typer.echo("No market data tick received within 10s.")
        typer.echo(f"valid_tick={bool(valid_tick)}")
        if not valid_tick and isinstance(broker, IBKRBroker):
            errors = broker.recent_errors(limit=5)
            if errors:
                typer.echo("recent_error_events:")
                for err in errors:
                    typer.echo(
                        f"  ts={err.get('ts')} reqId={err.get('reqId')} code={err.get('errorCode')} msg={err.get('errorString')}"
                    )

    except Exception as exc:
        log.error("doctor_failed", extra={"error": str(exc)})
        typer.echo(f"Doctor failed: {exc}")
        raise typer.Exit(code=1)
    finally:
        broker.disconnect()


@app.command()
def flatten(
    config: str = typer.Option(..., "--config", "-c"),
    confirm: bool = typer.Option(False, "--confirm", help="Required to execute flatten"),
    symbol: Optional[str] = typer.Option(None, "--symbol", help="Override instrument symbol"),
) -> None:
    """Safety tool: cancel all orders and flatten position via market order."""
    if not confirm:
        typer.echo("Refusing to run flatten without --confirm")
        raise typer.Exit(code=1)

    cfg: AppConfig = load_config(config)
    setup_logging(cfg.log)
    log = logging.getLogger("flatten")
    log.warning("FLATTEN - SAFETY TOOL", extra={"env": cfg.env})

    broker = _make_broker(cfg)
    db = Database(cfg.storage.sqlite_path)
    ledger = Ledger(price_multiplier=cfg.instrument.multiplier)

    sym = symbol or cfg.instrument.symbol

    def on_fill(fill: Fill) -> None:
        ledger.on_fill(fill)
        db.insert_fill(fill)
        db.update_order_status(fill.client_order_id, "Filled", fill.broker_order_id)

    def on_status(update: OrderStatusUpdate) -> None:
        db.update_order_status(update.client_order_id, update.status, update.broker_order_id)

    try:
        broker.connect()

        # cancel existing open orders from DB
        for order in db.get_open_orders():
            if order.symbol != sym:
                continue
            if order.broker_order_id:
                broker.cancel_order(order.broker_order_id)
            db.update_order_status(order.client_order_id, "Cancelled")

        broker.cancel_all_orders(sym)

        positions = broker.get_positions()
        pos_qty = 0
        for p in positions:
            if p.symbol == sym:
                pos_qty = p.qty
                break

        if pos_qty == 0:
            typer.echo(f"Already flat on {sym}")
            return

        side = "SELL" if pos_qty > 0 else "BUY"
        qty = abs(pos_qty)
        intent = OrderIntent(
            ts=datetime.utcnow(),
            symbol=sym,
            side=side,
            qty=qty,
            order_type="MKT",
            client_order_id=_make_client_order_id(),
        )

        db.insert_order(intent, status="Created")
        ack = broker.place_order(intent)
        db.update_order_ack(intent.client_order_id, ack.broker_order_id, ack.status or "Submitted")
        typer.echo(f"submitted flatten order broker_id={ack.broker_order_id} side={side} qty={qty}")

        deadline = time.time() + 5
        while time.time() < deadline:
            broker.poll_order_status(on_status)
            broker.poll_fills(on_fill)
            time.sleep(0.1)

    except Exception as exc:
        log.error("flatten_failed", extra={"error": str(exc)})
        typer.echo(f"Flatten failed: {exc}")
        raise typer.Exit(code=1)
    finally:
        broker.disconnect()
        db.close()
