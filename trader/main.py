from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer

from trader.broker.base import Broker
from trader.bus import EventBus
from trader.config import AppConfig, load_config
from trader.events import Fill, MarketEvent, OrderIntent, OrderStatusUpdate
from trader.ledger import Ledger
from trader.logging_setup import setup_logging
from trader.persistence import Database
from trader.risk import RiskEngine
from trader.strategy import NoopStrategy, Strategy
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
            instrument=cfg.instrument,
        )
    raise ValueError(f"Unsupported broker type: {cfg.broker.type}")


@app.command()
def run(config: str = typer.Option(..., "--config", "-c"), iterations: int = typer.Option(0, "--iterations", "-n", help="0 = run forever")) -> None:
    """Run the bot loop (dry wiring by default)."""
    cfg: AppConfig = load_config(config)
    setup_logging(cfg.log)
    log = logging.getLogger("runner")
    log.info("starting", extra={"env": cfg.env, "symbol": cfg.instrument.symbol})

    db = Database(cfg.storage.sqlite_path)
    ledger = Ledger(price_multiplier=cfg.instrument.multiplier)
    risk = RiskEngine(cfg.risk)
    strat: Strategy = NoopStrategy()

    broker = _make_broker(cfg)

    last_market: Optional[MarketEvent] = None

    def on_market(evt: MarketEvent) -> None:
        nonlocal last_market
        last_market = evt
        log.info("market", extra={"symbol": evt.symbol, "bid": evt.bid, "ask": evt.ask, "last": evt.last})

        sig = strat.on_market(evt)
        if sig is None:
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

    def on_fill(fill: Fill) -> None:
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
        ledger.on_fill(fill)
        db.insert_fill(fill)

    def on_order_status(update: OrderStatusUpdate) -> None:
        db.update_order_status(update.client_order_id, update.status, update.broker_order_id)

    def on_commission(exec_id: str, commission: float) -> None:
        updated = db.update_fill_commission(exec_id, commission)
        if updated:
            log.info("commission_backfilled", extra={"exec_id": exec_id, "commission": commission})

    try:
        broker.connect()

        if isinstance(broker, IBKRBroker):
            reconcile_ibkr(broker, db, ledger, risk, cfg.instrument.symbol)
            if risk.halted:
                log.error("halted_after_reconcile")
                return

        # MVP: a tiny loop that:
        # - emits a few market events
        # - polls fills
        # - writes pnl snapshots
        i = 0
        while True:
            broker.subscribe_market_data(cfg.instrument.symbol, on_market)
            broker.poll_order_status(on_order_status)
            broker.poll_fills(on_fill)
            broker.poll_commissions(on_commission)

            last_price = last_market.last if last_market else None
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
        broker.disconnect()
        db.close()
        log.info("stopped")


@app.command()
def report(config: str = typer.Option(..., "--config", "-c"), symbol: str = typer.Option("MGC")) -> None:
    """Very small PnL series dump from sqlite."""
    cfg: AppConfig = load_config(config)
    setup_logging(cfg.log)
    from trader.reporting.daily_report import load_pnl_series

    series = load_pnl_series(cfg.storage.sqlite_path, symbol=symbol)
    for ts, pnl in series[-20:]:
        print(ts, pnl)


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
    fills: list[Fill] = []

    def on_market(evt: MarketEvent) -> None:
        nonlocal last_tick
        last_tick = evt

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
        while last_tick is None and time.time() < tick_deadline:
            broker.poll_fills(on_fill)  # pump event loop for ib_insync
            time.sleep(0.1)
        if last_tick is None:
            raise RuntimeError("No market data received within 15 seconds")

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
        snap = ledger.snapshot(cfg.instrument.symbol, last_tick.last)
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
        accounts_raw = getattr(ib, "managedAccounts", []) or []
        accounts_list = (
            accounts_raw.split(",") if isinstance(accounts_raw, str) else list(accounts_raw)
        )
        now_ib = ib.reqCurrentTime()

        typer.echo(f"serverVersion={server_version}")
        typer.echo(f"managedAccounts={accounts_list}")
        typer.echo(f"currentTime={now_ib}")

        contracts = broker.contract_candidates(limit=5)  # type: ignore[attr-defined]
        typer.echo("nearest_contracts:")
        for c in contracts:
            typer.echo(f"  {c.lastTradeDateOrContractMonth} conId={c.conId}")

        first_tick: Optional[MarketEvent] = None

        def on_market(evt: MarketEvent) -> None:
            nonlocal first_tick
            if first_tick is None:
                first_tick = evt

        broker.subscribe_market_data(cfg.instrument.symbol, on_market)
        deadline = time.time() + 10
        while first_tick is None and time.time() < deadline:
            broker.poll_fills(lambda _fill: None)
            time.sleep(0.1)

        if first_tick:
            typer.echo(
                f"tick: bid={first_tick.bid} ask={first_tick.ask} last={first_tick.last} ts={first_tick.ts}"
            )
        else:
            typer.echo("No market data tick received within 10s.")

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
