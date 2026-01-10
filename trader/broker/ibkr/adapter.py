from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Deque, Dict, Optional, Set, Tuple
import uuid

from ib_insync import IB, BarData, Future, LimitOrder, MarketOrder, Ticker

from trader.broker.base import Broker
from trader.config import DataConfig, InstrumentConfig, OrderConfig
from trader.events import BarEvent, Fill, MarketEvent, OrderAck, OrderIntent, OrderStatusUpdate
from trader.market_data import normalize_price
from trader.models import Position


def classify_ibkr_event(error_code: int, error_string: str) -> int:
    """Map IBKR error codes to logging levels."""
    ok_codes = {2104, 2106, 2158}
    info_codes = ok_codes | {1101, 1102}
    warning_codes = {10349, 1100}

    if error_code in info_codes:
        return logging.INFO
    if error_code in warning_codes:
        return logging.WARNING
    return logging.ERROR


class IBKRBroker(Broker):
    """Interactive Brokers adapter built on ib_insync (sync event pump)."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 1,
        market_data_type: int = 3,
        connect_timeout_seconds: int = 20,
        instrument: Optional[InstrumentConfig] = None,
        orders: Optional[OrderConfig] = None,
        data: Optional[DataConfig] = None,
        instance_id: str = "bot1",
    ) -> None:
        self._log = logging.getLogger("broker.ibkr")
        self.host = host
        self.port = port
        self.client_id = client_id
        self.market_data_type = market_data_type  # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen
        self._instrument = instrument
        self._orders = orders or OrderConfig()
        self._data = data or DataConfig()
        self._instance_id = instance_id or "bot1"
        self.connect_timeout_seconds = connect_timeout_seconds

        self._connected = False
        self._ib: Optional[IB] = None
        self._contract: Optional[Future] = None
        self._ticker: Optional[Ticker] = None
        self._ticker_cb: Optional[Callable[[Ticker], None]] = None
        self._seen_exec_ids: Set[str] = set()
        self._client_to_broker: Dict[str, str] = {}
        self._broker_to_client: Dict[str, str] = {}
        self._seen_status_keys: Set[Tuple[str, str, float]] = set()
        self._seen_commission_ids: Set[str] = set()
        self._recent_errors: Deque[dict] = deque(maxlen=50)
        self._error_cb: Optional[Callable[..., None]] = None
        self._bars_cb: Optional[Callable[[BarEvent], None]] = None
        self._bars_subscription: Optional[object] = None
        self._bars_handler: Optional[Callable[[object], None]] = None
        self._last_bar_ts: Optional[str] = None
        self._seen_bar_keys: Set[Tuple[str, str]] = set()
        self._bar_snapshot_limit: int = 500
        self._bar_poll_contract: Optional[Future] = None
        self._last_bar_poll: float = 0.0
        self._bar_poll_interval: float = 5.0

    def _ensure_connected(self) -> IB:
        if not self._connected or self._ib is None:
            raise RuntimeError("IBKRBroker not connected")
        return self._ib

    def connect(self) -> None:
        self._ib = IB()
        self._recent_errors.clear()
        self._error_cb = self._on_error
        self._ib.errorEvent += self._error_cb  # type: ignore[assignment]
        try:
            self._ib.connect(self.host, self.port, clientId=self.client_id, timeout=self.connect_timeout_seconds)
        except Exception as exc:  # pragma: no cover - network/IB errors
            self._detach_error_handler()
            self._log.error(
                "connect_failed",
                extra={
                    "host": self.host,
                    "port": self.port,
                    "client_id": self.client_id,
                    "timeout": self.connect_timeout_seconds,
                    "market_data_type": self.market_data_type,
                    "error": str(exc),
                },
            )
            raise RuntimeError(f"IBKR connect failed: {exc}") from exc
        if not self._ib.isConnected():
            msg = f"IBKR connect failed: not connected (host={self.host} port={self.port} client_id={self.client_id})"
            self._detach_error_handler()
            self._log.error(
                "connect_failed",
                extra={
                    "host": self.host,
                    "port": self.port,
                    "client_id": self.client_id,
                    "timeout": self.connect_timeout_seconds,
                    "market_data_type": self.market_data_type,
                },
            )
            raise RuntimeError(msg)
        self._ib.reqMarketDataType(self.market_data_type)
        self._connected = True
        self._log.info(
            "connected",
            extra={
                "host": self.host,
                "port": self.port,
                "client_id": self.client_id,
                "timeout": self.connect_timeout_seconds,
                "market_data_type": self.market_data_type,
            },
        )

    def disconnect(self) -> None:
        if self._ib is None:
            return

        self._detach_error_handler()

        if self._ticker is not None:
            try:
                self._ib.cancelMktData(self._ticker.contract)
            except Exception:
                pass
            if self._ticker_cb:
                try:
                    self._ticker.updateEvent -= self._ticker_cb  # type: ignore[operator]
                except Exception:
                    pass
            self._ticker = None
            self._ticker_cb = None

        if self._bars_subscription is not None and self._bars_handler is not None:
            try:
                self._bars_subscription.updateEvent -= self._bars_handler  # type: ignore[operator]
            except Exception:
                pass
        self._bars_subscription = None
        self._bars_handler = None
        self._bars_cb = None
        self._seen_bar_keys.clear()
        self._bar_poll_contract = None

        self._ib.disconnect()
        self._connected = False
        self._log.info("disconnected")

    def _resolve_contract(self) -> Future:
        if self._contract is not None:
            return self._contract

        if self._instrument is None:
            raise RuntimeError("Instrument config required to resolve IBKR contract")

        ib = self._ensure_connected()
        inst = self._instrument

        if inst.expiry:
            contract = Future(
                symbol=inst.symbol,
                lastTradeDateOrContractMonth=inst.expiry,
                exchange=inst.exchange,
                currency=inst.currency,
            )
            qualified = ib.qualifyContracts(contract)
            if not qualified:
                raise RuntimeError(f"Failed to qualify IBKR contract for {inst.symbol} {inst.expiry}")
            self._contract = qualified[0]
            self._log.info(
                "contract_selected",
                extra={
                    "symbol": inst.symbol,
                    "expiry": self._contract.lastTradeDateOrContractMonth,
                    "conId": self._contract.conId,
                },
            )
            return self._contract

        base = Future(symbol=inst.symbol, exchange=inst.exchange, currency=inst.currency)
        details = ib.reqContractDetails(base)
        expiries: list[str] = []
        candidates: list[Tuple[int, Future]] = []
        now = datetime.utcnow()
        current_month = now.year * 100 + now.month

        for detail in details:
            expiry = (detail.contract.lastTradeDateOrContractMonth or "").strip()
            if not expiry:
                continue
            expiries.append(expiry)
            try:
                expiry_month = int(expiry[:6])
            except ValueError:
                continue
            if expiry_month >= current_month:
                candidates.append((expiry_month, detail.contract))

        if not candidates:
            self._log.error("no_nonexpired_contract", extra={"symbol": inst.symbol, "expiries": expiries[:5]})
            raise RuntimeError(f"No non-expired futures for {inst.symbol}; expiries={expiries[:5]}")

        candidates.sort(key=lambda item: item[0])
        selected = candidates[0][1]
        qualified = ib.qualifyContracts(selected)
        if qualified:
            selected = qualified[0]

        self._contract = selected
        self._log.info(
            "contract_selected",
            extra={
                "symbol": inst.symbol,
                "expiry": selected.lastTradeDateOrContractMonth,
                "conId": selected.conId,
            },
        )
        return selected

    def subscribe_market_data(self, symbol: str, on_event: Callable[[MarketEvent], None]) -> None:
        ib = self._ensure_connected()
        if self._ticker is not None:
            return

        contract = self._resolve_contract()
        ticker = ib.reqMktData(contract, "", False, False)

        def _on_update(_: Ticker) -> None:
            bid = normalize_price(ticker.bid)
            ask = normalize_price(ticker.ask)
            last = normalize_price(ticker.last)
            evt = MarketEvent(ts=datetime.utcnow(), symbol=symbol, bid=bid, ask=ask, last=last)
            on_event(evt)

        ticker.updateEvent += _on_update  # type: ignore[operator]
        self._ticker = ticker
        self._ticker_cb = _on_update
        self._log.info("market_data_subscribed", extra={"symbol": symbol, "conId": contract.conId})

    def subscribe_bars(self, symbol: str, on_bar: Callable[[BarEvent], None]) -> None:
        ib = self._ensure_connected()
        if self._bars_cb is not None:
            return
        contract = self._resolve_contract()
        self._bars_cb = on_bar
        self._last_bar_ts = None
        self._seen_bar_keys.clear()
        self._bar_poll_contract = None
        snapshot_bars = []
        try:
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=self._data.duration,
                barSizeSetting=self._data.bar_size,
                whatToShow="TRADES",
                useRTH=self._data.use_rth,
                formatDate=1,
                keepUpToDate=True,
            )
            self._bars_subscription = bars
            snapshot_bars = list(bars)

            def _on_update(_bars: object, _has_new_bar: object = None) -> None:
                if not bars:
                    return
                self._emit_bar(symbol, bars[-1])

            bars.updateEvent += _on_update  # type: ignore[operator]
            self._bars_handler = _on_update
            self._log.info(
                "bars_subscribed",
                extra={
                    "symbol": symbol,
                    "bar_size": self._data.bar_size,
                    "duration": self._data.duration,
                    "use_rth": self._data.use_rth,
                },
            )
        except TypeError:
            # keepUpToDate not supported; fallback to polling.
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=self._data.duration,
                barSizeSetting=self._data.bar_size,
                whatToShow="TRADES",
                useRTH=self._data.use_rth,
                formatDate=1,
                keepUpToDate=False,
            )
            snapshot_bars = list(bars) if bars else []
            self._bar_poll_contract = contract
            self._log.info(
                "bars_polling_enabled",
                extra={
                    "symbol": symbol,
                    "bar_size": self._data.bar_size,
                    "duration": self._data.duration,
                    "use_rth": self._data.use_rth,
                    "interval": self._bar_poll_interval,
                },
            )
        self._emit_bars_snapshot(symbol, snapshot_bars)

    def poll_bars(self) -> None:
        if self._bar_poll_contract is None or self._bars_cb is None or self._ib is None:
            return
        now = time.time()
        if now - self._last_bar_poll < self._bar_poll_interval:
            return
        self._last_bar_poll = now
        bars = self._ib.reqHistoricalData(
            self._bar_poll_contract,
            endDateTime="",
            durationStr=self._data.duration,
            barSizeSetting=self._data.bar_size,
            whatToShow="TRADES",
            useRTH=self._data.use_rth,
            formatDate=1,
            keepUpToDate=False,
        )
        if bars:
            self._emit_bar(self._bar_poll_contract.symbol, bars[-1])

    def place_order(self, intent: OrderIntent) -> OrderAck:
        ib = self._ensure_connected()
        contract = self._resolve_contract()

        tif = self._orders.default_tif
        outside_rth = bool(self._orders.outside_rth)

        if intent.order_type == "MKT":
            order = MarketOrder(intent.side, intent.qty)
        elif intent.order_type == "LMT":
            if intent.limit_price is None:
                raise ValueError("limit_price required for LMT orders")
            order = LimitOrder(intent.side, intent.qty, intent.limit_price)
        else:
            raise ValueError(f"Unsupported order type: {intent.order_type}")

        prefix = f"BOT:{self._instance_id}:"
        order.orderRef = f"{prefix}{intent.client_order_id}"
        order.tif = tif
        order.outsideRth = outside_rth
        self._log.info(
            "order_submit",
            extra={
                "client_order_id": intent.client_order_id,
                "symbol": intent.symbol,
                "side": intent.side,
                "qty": intent.qty,
                "order_type": intent.order_type,
                "tif": tif,
                "outsideRth": outside_rth,
            },
        )
        trade = ib.placeOrder(contract, order)

        for _ in range(20):
            ib.waitOnUpdate(0.05)
            if order.orderId:
                break

        broker_id = str(order.orderId if order.orderId is not None else trade.order.orderId)
        self._client_to_broker[intent.client_order_id] = broker_id
        self._broker_to_client[broker_id] = intent.client_order_id
        status = trade.orderStatus.status if trade.orderStatus and trade.orderStatus.status else ""

        return OrderAck(
            ts=datetime.utcnow(),
            client_order_id=intent.client_order_id,
            broker_order_id=broker_id,
            status=status,
        )

    def poll_fills(self, on_fill: Callable[[Fill], None]) -> None:
        ib = self._ensure_connected()
        ib.waitOnUpdate(timeout=0.1)

        for trade in ib.trades():
            broker_order_id = str(trade.order.orderId)
            order_ref = trade.order.orderRef or ""
            client_order_id = self._parse_client_order_id(order_ref)
            for fill in trade.fills:
                exec_id = fill.execution.execId
                if exec_id in self._seen_exec_ids:
                    continue
                self._seen_exec_ids.add(exec_id)

                side_raw = fill.execution.side.upper()
                side = "BUY" if side_raw in ("BUY", "BOT") else "SELL"
                qty = int(fill.execution.shares)
                price = float(fill.execution.price)
                commission = 0.0
                if fill.commissionReport and fill.commissionReport.commission is not None:
                    commission = float(fill.commissionReport.commission)

                symbol = trade.contract.symbol
                fill_evt = Fill(
                    ts=datetime.utcnow(),
                    client_order_id=client_order_id,
                    broker_order_id=broker_order_id,
                    exec_id=exec_id,
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    price=price,
                    commission=commission,
                )
                on_fill(fill_evt)

    def poll_order_status(self, on_status: Callable[[OrderStatusUpdate], None]) -> None:
        ib = self._ensure_connected()
        ib.waitOnUpdate(timeout=0.0)

        for trade in ib.trades():
            broker_order_id = str(trade.order.orderId)
            order_ref = trade.order.orderRef or ""
            client_order_id = self._parse_client_order_id(order_ref)
            if trade.orderStatus is None:
                continue
            status = trade.orderStatus.status or ""
            filled = float(trade.orderStatus.filled or 0.0)
            remaining = float(trade.orderStatus.remaining or 0.0)
            key = (broker_order_id, status, filled)
            if key in self._seen_status_keys:
                continue
            self._seen_status_keys.add(key)
            on_status(
                OrderStatusUpdate(
                    ts=datetime.utcnow(),
                    client_order_id=client_order_id,
                    broker_order_id=broker_order_id,
                    status=status,
                    filled=filled,
                    remaining=remaining,
                )
            )

    def cancel_order(self, broker_order_id: str) -> None:
        ib = self._ensure_connected()
        for trade in ib.trades():
            if trade.order and str(trade.order.orderId) == str(broker_order_id):
                ib.cancelOrder(trade.order)
                self._log.info("order_cancelled", extra={"broker_order_id": broker_order_id})
                return
        self._log.warning("order_cancel_not_found", extra={"broker_order_id": broker_order_id})

    def cancel_all_orders(self, symbol: Optional[str] = None) -> None:
        ib = self._ensure_connected()
        for trade in ib.openTrades():
            if symbol and getattr(trade.contract, "symbol", None) != symbol:
                continue
            ib.cancelOrder(trade.order)
        for order in ib.openOrders():
            # openOrders returns bare Order objects (no contract); cancel regardless if symbol filter is set.
            if symbol is None:
                ib.cancelOrder(order)
            else:
                ib.cancelOrder(order)
        self._log.info("cancel_all_orders", extra={"symbol": symbol})

    def poll_commissions(self, on_commission: Callable[[str, float], None]) -> None:
        ib = self._ensure_connected()
        ib.waitOnUpdate(timeout=0.0)

        for trade in ib.trades():
            for fill in trade.fills:
                exec_id = fill.execution.execId
                if exec_id in self._seen_commission_ids:
                    continue
                if fill.commissionReport and fill.commissionReport.commission:
                    commission = float(fill.commissionReport.commission)
                    on_commission(exec_id, commission)
                    self._seen_commission_ids.add(exec_id)

    def get_positions(self) -> list[Position]:
        ib = self._ensure_connected()
        positions: list[Position] = []
        for p in ib.positions():
            try:
                positions.append(Position(symbol=p.contract.symbol, qty=int(p.position), avg_price=float(p.avgCost)))
            except Exception:
                continue
        return positions

    def flatten(self, symbol: str) -> None:
        self.cancel_all_orders(symbol)
        positions = self.get_positions()
        pos_qty = 0
        for p in positions:
            if p.symbol == symbol:
                pos_qty = p.qty
                break
        if pos_qty == 0:
            self._log.info("flatten_already_flat", extra={"symbol": symbol})
            return

        side = "SELL" if pos_qty > 0 else "BUY"
        intent = OrderIntent(
            ts=datetime.utcnow(),
            symbol=symbol,
            side=side,
            qty=abs(pos_qty),
            order_type="MKT",
            client_order_id=f"flatten-{uuid.uuid4().hex[:8]}",
        )
        self.place_order(intent)

    def raw_ib(self) -> IB:
        """Expose underlying IB client for read-only inspection flows (doctor)."""
        return self._ensure_connected()

    def recent_errors(self, limit: int = 5) -> list[dict]:
        """Return the most recent IBKR error events (up to limit)."""
        return list(self._recent_errors)[-limit:]

    def _on_error(self, req_id: int, error_code: int, error_string: str, contract: Optional[Future] = None) -> None:
        level = classify_ibkr_event(error_code, error_string)
        err = {
            "ts": datetime.utcnow(),
            "reqId": req_id,
            "errorCode": error_code,
            "errorString": error_string,
        }
        if level >= logging.WARNING:
            self._recent_errors.append(err)
        self._log.log(level, "ib_event", extra={"reqId": req_id, "errorCode": error_code, "errorString": error_string})

    def _parse_client_order_id(self, order_ref: str) -> str:
        prefix = f"BOT:{self._instance_id}:"
        if order_ref.startswith(prefix):
            return order_ref[len(prefix) :]
        return "UNKNOWN"

    def _normalize_bar_ts(self, raw_ts: object) -> datetime:
        ts = raw_ts if isinstance(raw_ts, datetime) else None
        if ts is None:
            try:
                ts = datetime.strptime(str(raw_ts), "%Y%m%d %H:%M:%S")
            except Exception:
                ts = datetime.utcnow().replace(tzinfo=timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        return ts

    def _bar_event_from_ib(self, symbol: str, bar: BarData, is_snapshot: bool = False) -> Optional[BarEvent]:
        try:
            ts = self._normalize_bar_ts(getattr(bar, "date", None))
            ts_key = ts.isoformat()
            key = (symbol, ts_key)
            if key in self._seen_bar_keys:
                return None
            self._seen_bar_keys.add(key)
            self._last_bar_ts = ts_key
            return BarEvent(
                ts_utc=ts,
                symbol=symbol,
                open=float(bar.open),
                high=float(bar.high),
                low=float(bar.low),
                close=float(bar.close),
                volume=float(bar.volume),
                is_snapshot=is_snapshot,
            )
        except Exception as exc:
            self._log.error("bar_parse_failed", extra={"symbol": symbol, "error": str(exc)})
            return None

    def _emit_bars_snapshot(self, symbol: str, bars: list[BarData]) -> int:
        if self._bars_cb is None or not bars:
            return 0
        emitted = 0
        for bar in bars[-self._bar_snapshot_limit :]:
            evt = self._bar_event_from_ib(symbol, bar, is_snapshot=True)
            if evt is None:
                continue
            self._bars_cb(evt)
            emitted += 1
        if emitted:
            self._log.info("bars_snapshot_loaded", extra={"symbol": symbol, "count": emitted})
        return emitted

    def _emit_bar(self, symbol: str, bar: BarData) -> None:
        if self._bars_cb is None or bar is None:
            return
        evt = self._bar_event_from_ib(symbol, bar)
        if evt:
            self._bars_cb(evt)

    def _detach_error_handler(self) -> None:
        if self._ib and self._error_cb:
            try:
                self._ib.errorEvent -= self._error_cb  # type: ignore[assignment]
            except Exception:
                pass
        self._error_cb = None

    def contract_candidates(self, limit: int = 5) -> list[Future]:
        """Return nearest contract month candidates for the configured instrument."""
        ib = self._ensure_connected()
        if self._instrument is None:
            raise RuntimeError("Instrument config required to list IBKR contracts")

        inst = self._instrument
        base = Future(symbol=inst.symbol, exchange=inst.exchange, currency=inst.currency)
        details = ib.reqContractDetails(base)

        def _expiry_key(expiry: str) -> Tuple[int, str]:
            try:
                return (int(expiry[:6]), expiry)
            except Exception:
                return (999999, expiry or "")

        uniq: Dict[int, Future] = {}
        for detail in details:
            contract = detail.contract
            if contract.conId in uniq:
                continue
            uniq[contract.conId] = contract

        sorted_contracts = sorted(
            uniq.values(),
            key=lambda c: _expiry_key(c.lastTradeDateOrContractMonth or ""),
        )
        return sorted_contracts[:limit]
