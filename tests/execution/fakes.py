"""Builders and fakes for execution tests. No network, no real credentials."""

from __future__ import annotations

from decimal import Decimal

from nomicronix.execution.models import (
    ExchangeError,
    ExchangeOrder,
    IntentState,
    OrderIntent,
    OrderStatus,
    Ticker,
    intent_id_for,
)

NOW_MS = 1_735_693_200_000  # 2025-01-01T01:00:00Z, the default signal close
HOUR_MS = 3_600_000


def make_intent(
    *,
    signal_id: str = "sig-1",
    symbol: str = "BTC/USDT",
    quantity: str = "1",
    entry: str = "100",
    stop: str = "95",
    target: str = "115",
    approved_notional: str = "100",
    created_at_ms: int = NOW_MS,
    expires_at_ms: int = NOW_MS + HOUR_MS,
    state: IntentState = IntentState.SHADOW,
    filled_quantity: str = "0",
    average_fill_price: str | None = None,
    exchange_order_id: str | None = None,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id_for(signal_id),
        signal_id=signal_id,
        symbol=symbol,
        side="buy",
        quantity=Decimal(quantity),
        entry_price=Decimal(entry),
        stop_price=Decimal(stop),
        target_price=Decimal(target),
        approved_notional=Decimal(approved_notional),
        created_at_ms=created_at_ms,
        expires_at_ms=expires_at_ms,
        state=state,
        filled_quantity=Decimal(filled_quantity),
        average_fill_price=(None if average_fill_price is None else Decimal(average_fill_price)),
        exchange_order_id=exchange_order_id,
    )


def make_order(
    *,
    client_order_id: str = intent_id_for("sig-1"),
    exchange_order_id: str = "ex-1",
    symbol: str = "BTC/USDT",
    side: str = "buy",
    status: OrderStatus = OrderStatus.OPEN,
    quantity: str = "1",
    filled_quantity: str = "0",
    average_price: str | None = None,
    fee: str = "0",
    updated_at_ms: int = NOW_MS,
    raw_status: str = "open",
) -> ExchangeOrder:
    return ExchangeOrder(
        client_order_id=client_order_id,
        exchange_order_id=exchange_order_id,
        symbol=symbol,
        side=side,
        status=status,
        quantity=Decimal(quantity),
        filled_quantity=Decimal(filled_quantity),
        average_price=None if average_price is None else Decimal(average_price),
        fee=Decimal(fee),
        updated_at_ms=updated_at_ms,
        raw_status=raw_status,
    )


class FakeAdapter:
    """Scripted exchange adapter. Never touches the network."""

    def __init__(self, *, market_type: str = "spot", permissions: set[str] | None = None):
        self._market_type = market_type
        self._permissions = set() if permissions is None else set(permissions)
        self.tickers: dict[str, Ticker] = {}
        self.orders: dict[str, ExchangeOrder] = {}
        self.by_exchange_id: dict[str, ExchangeOrder] = {}
        self.balance_map: dict[str, Decimal] = {}
        self.queues: dict[str, list[ExchangeOrder]] = {}
        self.failures: dict[str, Exception] = {}
        self.private_calls: list[str] = []
        self.public_calls: list[str] = []
        self.create_calls: list[tuple[str, str, Decimal, str]] = []
        self._sequence = 0

    def set_ticker(
        self,
        symbol: str,
        *,
        bid: str = "100",
        ask: str = "100.1",
        last: str | None = None,
        at_ms: int = NOW_MS,
    ) -> None:
        self.tickers[symbol] = Ticker(
            symbol=symbol,
            bid=Decimal(bid),
            ask=Decimal(ask),
            last=Decimal(last if last is not None else ask),
            at_ms=at_ms,
        )

    def queue(self, client_order_id: str, *orders: ExchangeOrder) -> None:
        self.queues.setdefault(client_order_id, []).extend(orders)

    def _fail(self, name: str) -> None:
        failure = self.failures.get(name)
        if failure is not None:
            raise failure

    def market_type(self) -> str:
        return self._market_type

    def fetch_ticker(self, symbol: str) -> Ticker:
        self.public_calls.append(f"fetch_ticker:{symbol}")
        self._fail("fetch_ticker")
        ticker = self.tickers.get(symbol)
        if ticker is None:
            raise ExchangeError(f"no ticker for {symbol}", code="missing_ticker")
        return ticker

    def fetch_permissions(self) -> set[str]:
        self.private_calls.append("fetch_permissions")
        self._fail("fetch_permissions")
        return set(self._permissions)

    def create_market_order(
        self, symbol: str, side: str, quantity: Decimal, client_order_id: str
    ) -> ExchangeOrder:
        self.private_calls.append(f"create_market_order:{client_order_id}")
        self._fail("create_market_order")
        self.create_calls.append((symbol, side, quantity, client_order_id))
        existing = self.orders.get(client_order_id)
        if existing is not None:
            return existing
        queued = self.queues.get(client_order_id)
        if queued:
            order = queued.pop(0)
        else:
            self._sequence += 1
            order = make_order(
                client_order_id=client_order_id,
                exchange_order_id=f"ex-{self._sequence}",
                symbol=symbol,
                side=side,
                status=OrderStatus.CLOSED,
                quantity=str(quantity),
                filled_quantity=str(quantity),
                average_price="100",
                raw_status="closed",
            )
        self.orders[client_order_id] = order
        self.by_exchange_id[order.exchange_order_id] = order
        return order

    def fetch_order(self, order_id: str, symbol: str) -> ExchangeOrder:
        self.private_calls.append(f"fetch_order:{order_id}")
        self._fail("fetch_order")
        current = self.by_exchange_id.get(order_id)
        if current is None:
            raise ExchangeError(f"unknown exchange order {order_id}", code="unknown_order")
        queued = self.queues.get(current.client_order_id)
        if queued:
            current = queued.pop(0)
            self.orders[current.client_order_id] = current
            self.by_exchange_id[order_id] = current
        return current

    def cancel_order(self, order_id: str, symbol: str) -> ExchangeOrder:
        self.private_calls.append(f"cancel_order:{order_id}")
        self._fail("cancel_order")
        current = self.by_exchange_id.get(order_id)
        if current is None:
            raise ExchangeError(f"unknown exchange order {order_id}", code="unknown_order")
        canceled = make_order(
            client_order_id=current.client_order_id,
            exchange_order_id=current.exchange_order_id,
            symbol=current.symbol,
            side=current.side,
            status=OrderStatus.CANCELED,
            quantity=str(current.quantity),
            filled_quantity=str(current.filled_quantity),
            average_price=(None if current.average_price is None else str(current.average_price)),
            fee=str(current.fee),
            updated_at_ms=current.updated_at_ms + 1,
            raw_status="canceled",
        )
        self.orders[canceled.client_order_id] = canceled
        self.by_exchange_id[order_id] = canceled
        return canceled

    def open_orders(self, symbol: str | None = None) -> list[ExchangeOrder]:
        self.private_calls.append(f"open_orders:{symbol or '*'}")
        self._fail("open_orders")
        return [
            order
            for order in self.orders.values()
            if order.status is OrderStatus.OPEN and (symbol is None or order.symbol == symbol)
        ]

    def balances(self) -> dict[str, Decimal]:
        self.private_calls.append("balances")
        self._fail("balances")
        return dict(self.balance_map)

    def server_time_ms(self) -> int | None:
        return NOW_MS
