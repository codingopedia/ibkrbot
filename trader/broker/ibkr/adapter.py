from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Dict, Optional, Set, Tuple
import uuid

from ib_insync import IB, Future, LimitOrder, MarketOrder, Ticker

from trader.broker.base import Broker
from trader.config import InstrumentConfig
from trader.events import Fill, MarketEvent, OrderAck, OrderIntent, OrderStatusUpdate
from trader.models import Position


class IBKRBroker(Broker):
    """Interactive Brokers adapter built on ib_insync (sync event pump)."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 1,
        market_data_type: int = 3,
        instrument: Optional[InstrumentConfig] = None,
    ) -> None:
        self._log = logging.getLogger("broker.ibkr")
        self.host = host
        self.port = port
        self.client_id = client_id
        self.market_data_type = market_data_type  # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen
        self._instrument = instrument

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

    def _ensure_connected(self) -> IB:
        if not self._connected or self._ib is None:
            raise RuntimeError("IBKRBroker not connected")
        return self._ib

    def connect(self) -> None:
        self._ib = IB()
        try:
            self._ib.connect(self.host, self.port, clientId=self.client_id)
        except Exception as exc:  # pragma: no cover - network/IB errors
            self._log.error(
                "connect_failed",
                extra={"host": self.host, "port": self.port, "client_id": self.client_id, "error": str(exc)},
            )
            raise RuntimeError(f"IBKR connect failed: {exc}") from exc
        if not self._ib.isConnected():
            msg = f"IBKR connect failed: not connected (host={self.host} port={self.port} client_id={self.client_id})"
            self._log.error("connect_failed", extra={"host": self.host, "port": self.port, "client_id": self.client_id})
            raise RuntimeError(msg)
        self._ib.reqMarketDataType(self.market_data_type)
        self._connected = True
        self._log.info("connected", extra={"host": self.host, "port": self.port, "client_id": self.client_id})

    def disconnect(self) -> None:
        if self._ib is None:
            return

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
            bid = float(ticker.bid) if ticker.bid is not None else None
            ask = float(ticker.ask) if ticker.ask is not None else None
            last = float(ticker.last) if ticker.last is not None else None
            evt = MarketEvent(ts=datetime.utcnow(), symbol=symbol, bid=bid, ask=ask, last=last)
            on_event(evt)

        ticker.updateEvent += _on_update  # type: ignore[operator]
        self._ticker = ticker
        self._ticker_cb = _on_update
        self._log.info("market_data_subscribed", extra={"symbol": symbol, "conId": contract.conId})

    def place_order(self, intent: OrderIntent) -> OrderAck:
        ib = self._ensure_connected()
        contract = self._resolve_contract()

        if intent.order_type == "MKT":
            order = MarketOrder(intent.side, intent.qty)
        elif intent.order_type == "LMT":
            if intent.limit_price is None:
                raise ValueError("limit_price required for LMT orders")
            order = LimitOrder(intent.side, intent.qty, intent.limit_price)
        else:
            raise ValueError(f"Unsupported order type: {intent.order_type}")

        order.orderRef = intent.client_order_id
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
            client_order_id = trade.order.orderRef or self._broker_to_client.get(broker_order_id, "")
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
            client_order_id = trade.order.orderRef or self._broker_to_client.get(broker_order_id, "")
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
            if symbol and getattr(order.contract, "symbol", None) != symbol:
                continue
            ib.cancelOrder(order.order)
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
