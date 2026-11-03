from __future__ import annotations

import subprocess
import sys
from decimal import Decimal

import ccxt
import pytest

from nomicronix.execution.adapter import CcxtAdapter
from nomicronix.execution.models import ExchangeError, OrderStatus
from tests.execution.fakes import NOW_MS, FakeAdapter, make_order


class FakeClient:
    """The subset of the ccxt interface the adapter is allowed to use."""

    def __init__(self) -> None:
        self.orders: dict[str, dict[str, object]] = {}
        self.calls: list[tuple[object, ...]] = []
        self.raise_on: dict[str, Exception] = {}
        self.permissions_supported = True
        self.balance = {
            "BTC": {"free": 0.5, "used": 0.0, "total": 0.5},
            "USDT": {"free": 400.0, "used": 0.0, "total": 400.0},
        }

    def _raise(self, name: str) -> None:
        failure = self.raise_on.get(name)
        if failure is not None:
            raise failure

    def fetch_ticker(self, symbol: str) -> dict[str, object]:
        self._raise("fetch_ticker")
        self.calls.append(("fetch_ticker", symbol))
        return {
            "symbol": symbol,
            "bid": 100.1,
            "ask": 100.2,
            "last": 100.15,
            "timestamp": NOW_MS,
        }

    def create_order(
        self,
        symbol: str,
        type: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, object]:
        self._raise("create_order")
        self.calls.append(("create_order", symbol, type, side, amount, params))
        client_id = (params or {}).get("newClientOrderId", "")
        order: dict[str, object] = {
            "id": "ex-1",
            "clientOrderId": client_id,
            "symbol": symbol,
            "side": side,
            "status": "closed",
            "amount": amount,
            "filled": amount,
            "average": 100.2,
            "fee": {"cost": 0.1, "currency": "USDT"},
            "timestamp": NOW_MS,
            "info": {"status": "FILLED"},
        }
        self.orders["ex-1"] = order
        return order

    def fetch_order(self, order_id: str, symbol: str) -> dict[str, object]:
        self._raise("fetch_order")
        self.calls.append(("fetch_order", order_id, symbol))
        return self.orders[order_id]

    def cancel_order(self, order_id: str, symbol: str) -> dict[str, object]:
        self._raise("cancel_order")
        self.calls.append(("cancel_order", order_id, symbol))
        order = dict(self.orders[order_id])
        order["status"] = "canceled"
        self.orders[order_id] = order
        return order

    def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, object]]:
        self._raise("fetch_open_orders")
        self.calls.append(("fetch_open_orders", symbol))
        return [
            order
            for order in self.orders.values()
            if order["status"] == "open" and (symbol is None or order["symbol"] == symbol)
        ]

    def fetch_balance(self) -> dict[str, object]:
        self._raise("fetch_balance")
        self.calls.append(("fetch_balance",))
        return {
            "total": {asset: values["total"] for asset, values in self.balance.items()},
            "free": {asset: values["free"] for asset, values in self.balance.items()},
            "used": {asset: values["used"] for asset, values in self.balance.items()},
        }

    def fetch_permissions(self) -> dict[str, object]:
        if not self.permissions_supported:
            raise ccxt.NotSupported("permissions are not exposed")
        return {"permissions": ["spot", "withdraw"]}


def adapter(client: FakeClient | None = None) -> CcxtAdapter:
    return CcxtAdapter("binance", client=client or FakeClient())


def test_the_fake_satisfies_the_protocol() -> None:
    from nomicronix.execution.adapter import ExchangeAdapter

    assert isinstance(FakeAdapter(), ExchangeAdapter)


def test_domain_import_does_not_load_ccxt() -> None:
    code = (
        "import sys; import nomicronix.execution.models, nomicronix.execution.store; "
        "assert 'ccxt' not in sys.modules, 'domain imported ccxt'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_market_type_is_spot() -> None:
    assert adapter().market_type() == "spot"


def test_ticker_is_converted_to_decimal() -> None:
    ticker = adapter().fetch_ticker("BTC/USDT")
    assert ticker.symbol == "BTC/USDT"
    assert ticker.bid == Decimal("100.1")
    assert ticker.ask == Decimal("100.2")
    assert ticker.last == Decimal("100.15")
    assert ticker.at_ms == NOW_MS


def test_create_market_order_uses_the_client_order_id() -> None:
    client = FakeClient()
    order = adapter(client).create_market_order("BTC/USDT", "buy", Decimal("0.5"), "nx-sig-1")

    assert client.calls[0] == (
        "create_order",
        "BTC/USDT",
        "market",
        "buy",
        0.5,
        {"newClientOrderId": "nx-sig-1"},
    )
    assert order.client_order_id == "nx-sig-1"
    assert order.exchange_order_id == "ex-1"
    assert order.status is OrderStatus.CLOSED
    assert order.filled_quantity == Decimal("0.5")
    assert order.average_price == Decimal("100.2")
    assert order.fee == Decimal("0.1")
    assert order.raw_status == "FILLED"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("open", OrderStatus.OPEN),
        ("closed", OrderStatus.CLOSED),
        ("canceled", OrderStatus.CANCELED),
        ("cancelled", OrderStatus.CANCELED),
        ("rejected", OrderStatus.REJECTED),
        ("expired", OrderStatus.EXPIRED),
    ],
)
def test_every_terminal_status_maps(raw: str, expected: OrderStatus) -> None:
    client = FakeClient()
    client.orders["ex-1"] = {
        "id": "ex-1",
        "clientOrderId": "nx-sig-1",
        "symbol": "BTC/USDT",
        "side": "buy",
        "status": raw,
        "amount": 1.0,
        "filled": 0.0,
        "average": None,
        "fee": None,
        "timestamp": NOW_MS,
        "info": {"status": raw.upper()},
    }
    assert adapter(client).fetch_order("ex-1", "BTC/USDT").status is expected


def test_an_unknown_status_fails_closed() -> None:
    client = FakeClient()
    client.orders["ex-1"] = {
        "id": "ex-1",
        "clientOrderId": "nx-sig-1",
        "symbol": "BTC/USDT",
        "side": "buy",
        "status": "teleported",
        "amount": 1.0,
        "filled": 0.0,
        "average": None,
        "fee": None,
        "timestamp": NOW_MS,
        "info": {},
    }
    with pytest.raises(ExchangeError) as caught:
        adapter(client).fetch_order("ex-1", "BTC/USDT")
    assert caught.value.code == "unknown_order_status"


def test_network_errors_map_to_a_stable_code() -> None:
    client = FakeClient()
    client.raise_on["fetch_ticker"] = ccxt.NetworkError("socket closed")
    with pytest.raises(ExchangeError) as caught:
        adapter(client).fetch_ticker("BTC/USDT")
    assert caught.value.code == "network_error"
    assert caught.value.exit_code == 3


def test_rate_limits_map_to_a_stable_code() -> None:
    client = FakeClient()
    client.raise_on["create_order"] = ccxt.RateLimitExceeded("slow down")
    with pytest.raises(ExchangeError) as caught:
        adapter(client).create_market_order("BTC/USDT", "buy", Decimal("1"), "nx-1")
    assert caught.value.code == "rate_limit"


def test_exchange_errors_are_redacted() -> None:
    client = FakeClient()
    client.raise_on["create_order"] = ccxt.AuthenticationError(
        "bad request https://api.binance.com/order?api_key=SECRETKEY&signature=SIGNED"
    )
    with pytest.raises(ExchangeError) as caught:
        adapter(client).create_market_order("BTC/USDT", "buy", Decimal("1"), "nx-1")
    message = str(caught.value)
    assert caught.value.code == "exchange_rejected"
    assert "SECRETKEY" not in message
    assert "SIGNED" not in message
    assert "***" in message


def test_open_orders_and_cancel() -> None:
    client = FakeClient()
    client.orders["ex-1"] = {
        "id": "ex-1",
        "clientOrderId": "nx-sig-1",
        "symbol": "BTC/USDT",
        "side": "buy",
        "status": "open",
        "amount": 1.0,
        "filled": 0.0,
        "average": None,
        "fee": None,
        "timestamp": NOW_MS,
        "info": {"status": "NEW"},
    }
    exchange = adapter(client)
    assert [order.exchange_order_id for order in exchange.open_orders()] == ["ex-1"]
    canceled = exchange.cancel_order("ex-1", "BTC/USDT")
    assert canceled.status is OrderStatus.CANCELED


def test_balances_are_decimal() -> None:
    balances = adapter().balances()
    assert balances["BTC"] == Decimal("0.5")
    assert balances["USDT"] == Decimal("400.0")


def test_permissions_are_lowercased_and_optional() -> None:
    assert adapter().fetch_permissions() == {"spot", "withdraw"}
    client = FakeClient()
    client.permissions_supported = False
    assert adapter(client).fetch_permissions() == set()


def test_fake_adapter_submit_is_idempotent() -> None:
    fake = FakeAdapter()
    first = fake.create_market_order("BTC/USDT", "buy", Decimal("1"), "nx-sig-1")
    second = fake.create_market_order("BTC/USDT", "buy", Decimal("1"), "nx-sig-1")
    assert first == second
    assert len(fake.create_calls) == 2
    assert len(fake.orders) == 1


def test_fake_adapter_can_script_every_terminal_status() -> None:
    fake = FakeAdapter()
    fake.set_ticker("BTC/USDT")
    queued = make_order(
        status=OrderStatus.CANCELED,
        filled_quantity="0.4",
        average_price="100",
        raw_status="canceled",
    )
    fake.queue("nx-sig-1", queued)
    created = fake.create_market_order("BTC/USDT", "buy", Decimal("1"), "nx-sig-1")
    assert created.status is OrderStatus.CANCELED
    assert created.filled_quantity == Decimal("0.4")
