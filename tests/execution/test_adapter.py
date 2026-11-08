from __future__ import annotations

import subprocess
import sys
from decimal import Decimal

import ccxt
import pytest

from nomicronix.execution.adapter import (
    CcxtAdapter,
    _redact,
    build_adapter,
    credentials_from_env,
    masked_key,
)
from nomicronix.execution.config import ExecutionConfig
from nomicronix.execution.models import CredentialError, ExchangeError, OrderStatus, utc_now_ms
from tests.execution.fakes import NOW_MS, FakeAdapter, make_order


class FakeClient:
    """The subset of the ccxt interface the adapter is allowed to use."""

    def __init__(self) -> None:
        self.orders: dict[str, dict[str, object]] = {}
        self.calls: list[tuple[object, ...]] = []
        self.raise_on: dict[str, Exception] = {}
        self.permissions_supported = True
        self.permissions: set[str] = {"spot", "withdraw"}
        self.options: dict[str, object] = {"defaultType": "spot"}
        self.time_ms: int | None = NOW_MS
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
        self._raise("fetch_permissions")
        return {"permissions": sorted(self.permissions)}

    def fetch_time(self) -> int:
        self._raise("fetch_time")
        if self.time_ms is None:
            raise ccxt.NotSupported("time is not exposed")
        return self.time_ms


def adapter(client: FakeClient | None = None) -> CcxtAdapter:
    return CcxtAdapter("binance", client=client or FakeClient())


def test_the_fake_satisfies_the_protocol() -> None:
    from nomicronix.execution.adapter import ExchangeAdapter

    assert isinstance(FakeAdapter(), ExchangeAdapter)


def test_domain_import_does_not_load_ccxt() -> None:
    code = (
        "import sys; "
        "import nomicronix.execution.models, nomicronix.execution.store, "
        "nomicronix.execution.config, nomicronix.execution.service, "
        "nomicronix.execution.shadow, nomicronix.execution.submit, "
        "nomicronix.execution.fills, nomicronix.execution.reconcile, "
        "nomicronix.execution.flatten; "
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


@pytest.mark.parametrize(
    "message,secret",
    [
        ("apiKey=SECRET", "SECRET"),
        ("apiSecret=SECRET", "SECRET"),
        ('{"apiKey": "SECRET"}', "SECRET"),
        ("signature: SECRET", "SECRET"),
        ("token=SECRET&x=1", "SECRET"),
    ],
)
def test_credential_shapes_are_redacted(message: str, secret: str) -> None:
    redacted = _redact(message)
    assert secret not in redacted
    assert "***" in redacted


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


def raw_order(**overrides: object) -> dict[str, object]:
    order: dict[str, object] = {
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
    order.update(overrides)
    return order


def test_market_type_falls_back_to_spot_without_options() -> None:
    class Bare:
        def fetch_ticker(self, symbol: str) -> dict[str, object]:
            return {}

    assert CcxtAdapter("binance", client=Bare()).market_type() == "spot"


def test_a_ticker_missing_a_side_fails_closed() -> None:
    client = FakeClient()
    client.fetch_ticker = lambda symbol: {"bid": 1, "ask": 2, "last": None}  # type: ignore[method-assign]
    with pytest.raises(ExchangeError) as caught:
        adapter(client).fetch_ticker("BTC/USDT")
    assert caught.value.code == "bad_ticker"


def test_ticker_uses_the_local_clock_when_timestamp_is_absent() -> None:
    client = FakeClient()

    def ticker(symbol: str) -> dict[str, object]:
        return {"symbol": symbol, "bid": 1, "ask": 2, "last": 1.5}

    client.fetch_ticker = ticker  # type: ignore[method-assign]
    assert adapter(client).fetch_ticker("BTC/USDT").at_ms > 0


def test_permissions_return_an_empty_set_when_absent_or_malformed() -> None:
    client = FakeClient()
    client.fetch_permissions = None  # type: ignore[assignment]
    assert adapter(client).fetch_permissions() == set()

    malformed = FakeClient()
    malformed.fetch_permissions = lambda: {"permissions": "spot"}  # type: ignore[method-assign]
    assert adapter(malformed).fetch_permissions() == set()


def test_permission_probe_errors_are_treated_as_unknown() -> None:
    client = FakeClient()
    client.raise_on["fetch_permissions"] = ccxt.NetworkError("offline")
    assert adapter(client).fetch_permissions() == set()


def test_every_adapter_operation_maps_network_errors() -> None:
    client = FakeClient()
    client.raise_on.update(
        {
            "fetch_order": ccxt.NetworkError("offline"),
            "cancel_order": ccxt.NetworkError("offline"),
            "fetch_open_orders": ccxt.NetworkError("offline"),
            "fetch_balance": ccxt.NetworkError("offline"),
        }
    )
    exchange = adapter(client)
    for call in (
        lambda: exchange.fetch_order("ex-1", "BTC/USDT"),
        lambda: exchange.cancel_order("ex-1", "BTC/USDT"),
        lambda: exchange.open_orders(),
        lambda: exchange.balances(),
    ):
        with pytest.raises(ExchangeError) as caught:
            call()
        assert caught.value.code == "network_error"


def test_an_order_without_amount_uses_the_filled_quantity() -> None:
    client = FakeClient()
    client.orders["ex-1"] = raw_order(amount=None, filled=1.0, average=100.0, status="closed")
    order = adapter(client).fetch_order("ex-1", "BTC/USDT")
    assert order.quantity == Decimal("1.0")


def test_an_order_without_quantity_fails_closed() -> None:
    client = FakeClient()
    client.orders["ex-1"] = raw_order(amount=None, filled=0.0)
    with pytest.raises(ExchangeError) as caught:
        adapter(client).fetch_order("ex-1", "BTC/USDT")
    assert caught.value.code == "bad_order"


def test_an_order_filled_beyond_its_amount_fails_closed() -> None:
    client = FakeClient()
    client.orders["ex-1"] = raw_order(amount=1.0, filled=2.0, average=100.0)
    with pytest.raises(ExchangeError) as caught:
        adapter(client).fetch_order("ex-1", "BTC/USDT")
    assert caught.value.code == "bad_order"


def test_average_is_derived_from_cost_when_missing() -> None:
    client = FakeClient()
    client.orders["ex-1"] = raw_order(
        filled=2.0, amount=2.0, average=None, price=None, cost=200.0, status="closed"
    )
    order = adapter(client).fetch_order("ex-1", "BTC/USDT")
    assert order.average_price == Decimal("100")


def test_a_fill_without_any_price_fails_closed() -> None:
    client = FakeClient()
    client.orders["ex-1"] = raw_order(
        filled=1.0, amount=1.0, average=None, price=None, cost=None, status="closed"
    )
    with pytest.raises(ExchangeError) as caught:
        adapter(client).fetch_order("ex-1", "BTC/USDT")
    assert caught.value.code == "unknown_order_fill"


def test_fee_lists_are_summed() -> None:
    client = FakeClient()
    client.orders["ex-1"] = raw_order(
        filled=1.0,
        amount=1.0,
        average=100.0,
        status="closed",
        fee=[{"cost": 0.1}, {"cost": 0.2}, {"currency": "USDT"}, "junk"],
    )
    assert adapter(client).fetch_order("ex-1", "BTC/USDT").fee == Decimal("0.3")


def test_an_order_without_an_exchange_id_fails_closed() -> None:
    client = FakeClient()
    client.orders["ex-1"] = raw_order(id=None)
    with pytest.raises(ExchangeError) as caught:
        adapter(client).fetch_order("ex-1", "BTC/USDT")
    assert caught.value.code == "bad_order"


def test_an_order_without_any_client_id_fails_closed() -> None:
    client = FakeClient()
    client.orders["ex-1"] = raw_order(clientOrderId=None, info={})
    with pytest.raises(ExchangeError) as caught:
        adapter(client).fetch_order("ex-1", "BTC/USDT")
    assert caught.value.code == "bad_order"


def test_client_id_can_come_from_info_and_timestamp_from_the_last_trade() -> None:
    client = FakeClient()
    client.orders["ex-1"] = raw_order(
        clientOrderId=None,
        timestamp=None,
        lastTradeTimestamp=NOW_MS + 5,
        info={"status": "NEW", "clientOrderId": "nx-from-info"},
    )
    order = adapter(client).fetch_order("ex-1", "BTC/USDT")
    assert order.client_order_id == "nx-from-info"
    assert order.updated_at_ms == NOW_MS + 5
    assert order.raw_status == "NEW"


def test_balances_require_a_total_map_and_skip_nulls() -> None:
    client = FakeClient()
    client.fetch_balance = lambda: {}  # type: ignore[method-assign]
    with pytest.raises(ExchangeError) as caught:
        adapter(client).balances()
    assert caught.value.code == "bad_balance"

    client.fetch_balance = lambda: {"total": {"BTC": 0.5, "ETH": None}}  # type: ignore[method-assign]
    assert adapter(client).balances() == {"BTC": Decimal("0.5")}


def test_server_time_handles_missing_method_and_not_supported() -> None:
    class NoTime:
        pass

    assert CcxtAdapter("binance", client=NoTime()).server_time_ms() is None

    client = FakeClient()
    client.time_ms = None
    assert adapter(client).server_time_ms() is None


def test_server_time_errors_are_mapped() -> None:
    client = FakeClient()
    client.raise_on["fetch_time"] = ccxt.NetworkError("offline")
    with pytest.raises(ExchangeError) as caught:
        adapter(client).server_time_ms()
    assert caught.value.code == "network_error"


def test_the_constructor_can_build_the_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def factory(options: dict[str, object]) -> FakeClient:
        captured.update(options)
        return FakeClient()

    monkeypatch.setattr(ccxt, "binance", factory, raising=False)
    exchange = CcxtAdapter("binance")
    assert exchange.market_type() == "spot"
    assert captured["options"] == {"defaultType": "spot"}


def test_credentials_are_read_only_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOMICRONIX_BINANCE_API_KEY", "abcd1234")
    monkeypatch.setenv("NOMICRONIX_BINANCE_API_SECRET", "shh")
    assert credentials_from_env() == ("abcd1234", "shh")
    assert masked_key("abcd1234") == "***1234"
    assert masked_key("abc") == "***"


def test_missing_credentials_are_a_credential_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOMICRONIX_BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("NOMICRONIX_BINANCE_API_SECRET", raising=False)
    with pytest.raises(CredentialError, match="missing exchange credentials"):
        credentials_from_env()


def install_exchange(monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> dict[str, object]:
    captured: dict[str, object] = {}

    def factory(options: dict[str, object]) -> FakeClient:
        captured.update(options)
        return client

    monkeypatch.setattr(ccxt, "binance", factory, raising=False)
    return captured


def test_build_adapter_requires_spot(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    client.options = {"defaultType": "future"}
    install_exchange(monkeypatch, client)
    config = ExecutionConfig()
    with pytest.raises(ExchangeError) as caught:
        build_adapter(config, require_private=False)
    assert caught.value.code == "non_spot_market"


def test_build_adapter_rejects_a_key_that_can_withdraw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOMICRONIX_BINANCE_API_KEY", "abcd1234")
    monkeypatch.setenv("NOMICRONIX_BINANCE_API_SECRET", "shh")
    client = FakeClient()
    install_exchange(monkeypatch, client)
    with pytest.raises(CredentialError, match="trade-only"):
        build_adapter(ExecutionConfig(), require_private=True)


def test_build_adapter_passes_keys_and_checks_the_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOMICRONIX_BINANCE_API_KEY", "abcd1234")
    monkeypatch.setenv("NOMICRONIX_BINANCE_API_SECRET", "shh")
    client = FakeClient()
    client.permissions = {"spot"}
    client.time_ms = utc_now_ms() + 10_000
    captured = install_exchange(monkeypatch, client)

    with pytest.raises(ExchangeError) as caught:
        build_adapter(ExecutionConfig(), require_private=True)
    assert caught.value.code == "clock_skew"
    assert captured["apiKey"] == "abcd1234"
    assert captured["apiSecret"] == "shh"


def test_build_adapter_returns_a_spot_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOMICRONIX_BINANCE_API_KEY", "abcd1234")
    monkeypatch.setenv("NOMICRONIX_BINANCE_API_SECRET", "shh")
    client = FakeClient()
    client.permissions = {"spot"}
    client.time_ms = utc_now_ms()
    install_exchange(monkeypatch, client)
    exchange = build_adapter(ExecutionConfig(), require_private=True)
    assert exchange.market_type() == "spot"


def test_build_adapter_rejects_an_unknown_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ccxt, "not-an-exchange", None, raising=False)
    with pytest.raises(ExchangeError) as caught:
        build_adapter(ExecutionConfig(exchange="not-an-exchange"), require_private=False)
    assert caught.value.code == "unknown_exchange"
