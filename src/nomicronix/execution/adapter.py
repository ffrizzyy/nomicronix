"""Exchange access for execution.

Only this module may import ``ccxt``, and only lazily. The domain and the tests
never touch the network. API keys are read from the environment and never
appear in an error message.
"""

from __future__ import annotations

import os
import re
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from nomicronix.execution.config import ExecutionConfig
from nomicronix.execution.models import (
    CredentialError,
    ExchangeError,
    ExchangeOrder,
    OrderStatus,
    Ticker,
    utc_now_ms,
)

API_KEY_ENV = "NOMICRONIX_BINANCE_API_KEY"
API_SECRET_ENV = "NOMICRONIX_BINANCE_API_SECRET"

_REDACT = re.compile(r"(api[_-]?key|signature|secret|token)=([^&\s]+)", re.IGNORECASE)


def _redact(message: str) -> str:
    return _REDACT.sub(r"\1=***", message)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value is None else _decimal(value)


@runtime_checkable
class ExchangeAdapter(Protocol):
    """The narrow exchange surface execution is allowed to use."""

    def market_type(self) -> str: ...

    def fetch_ticker(self, symbol: str) -> Ticker: ...

    def fetch_permissions(self) -> set[str]: ...

    def create_market_order(
        self, symbol: str, side: str, quantity: Decimal, client_order_id: str
    ) -> ExchangeOrder: ...

    def fetch_order(self, order_id: str, symbol: str) -> ExchangeOrder: ...

    def cancel_order(self, order_id: str, symbol: str) -> ExchangeOrder: ...

    def open_orders(self, symbol: str | None = None) -> list[ExchangeOrder]: ...

    def balances(self) -> dict[str, Decimal]: ...

    def server_time_ms(self) -> int | None: ...


class CcxtAdapter:
    """ExchangeAdapter backed by ccxt. Spot only; no withdrawal method exists."""

    def __init__(self, exchange_id: str = "binance", client: Any = None) -> None:
        self.name = exchange_id
        if client is None:
            import ccxt

            client = getattr(ccxt, exchange_id)(
                {"enableRateLimit": True, "options": {"defaultType": "spot"}}
            )
        self._client = client

    def market_type(self) -> str:
        options = getattr(self._client, "options", None)
        if isinstance(options, dict):
            return str(options.get("defaultType", "spot"))
        return "spot"

    def server_time_ms(self) -> int | None:
        method = getattr(self._client, "fetch_time", None)
        if method is None:
            return None
        try:
            return int(method())
        except Exception as exc:  # mapped below
            import ccxt

            if isinstance(exc, ccxt.NotSupported):
                return None
            raise _map_error(exc, "fetch_time") from exc

    def fetch_ticker(self, symbol: str) -> Ticker:
        try:
            raw = self._client.fetch_ticker(symbol)
        except Exception as exc:
            raise _map_error(exc, "fetch_ticker") from exc
        bid = _optional_decimal(raw.get("bid"))
        ask = _optional_decimal(raw.get("ask"))
        last = _optional_decimal(raw.get("last"))
        if bid is None or ask is None or last is None:
            raise ExchangeError(
                f"ticker for {symbol} is missing bid, ask or last", code="bad_ticker"
            )
        return Ticker(
            symbol=str(raw.get("symbol", symbol)),
            bid=bid,
            ask=ask,
            last=last,
            at_ms=int(raw.get("timestamp") or utc_now_ms()),
        )

    def fetch_permissions(self) -> set[str]:
        method = getattr(self._client, "fetch_permissions", None)
        if method is None:
            return set()
        try:
            raw = method()
        except Exception:
            # The exchange does not expose permissions, or the probe failed.
            # The caller prints a reminder and the trade-only key rule stands.
            return set()
        values = raw.get("permissions") if isinstance(raw, dict) else raw
        if not isinstance(values, (list, tuple, set)):
            return set()
        return {str(value).lower() for value in values}

    def create_market_order(
        self, symbol: str, side: str, quantity: Decimal, client_order_id: str
    ) -> ExchangeOrder:
        try:
            raw = self._client.create_order(
                symbol,
                "market",
                side,
                float(quantity),
                None,
                {"newClientOrderId": client_order_id},
            )
        except Exception as exc:
            raise _map_error(exc, "create_order") from exc
        return _order_from_ccxt(raw, fallback_client_id=client_order_id)

    def fetch_order(self, order_id: str, symbol: str) -> ExchangeOrder:
        try:
            raw = self._client.fetch_order(order_id, symbol)
        except Exception as exc:
            raise _map_error(exc, "fetch_order") from exc
        return _order_from_ccxt(raw)

    def cancel_order(self, order_id: str, symbol: str) -> ExchangeOrder:
        try:
            raw = self._client.cancel_order(order_id, symbol)
        except Exception as exc:
            raise _map_error(exc, "cancel_order") from exc
        return _order_from_ccxt(raw)

    def open_orders(self, symbol: str | None = None) -> list[ExchangeOrder]:
        try:
            raw = self._client.fetch_open_orders(symbol)
        except Exception as exc:
            raise _map_error(exc, "fetch_open_orders") from exc
        return [_order_from_ccxt(order) for order in raw]

    def balances(self) -> dict[str, Decimal]:
        try:
            raw = self._client.fetch_balance()
        except Exception as exc:
            raise _map_error(exc, "fetch_balance") from exc
        totals = raw.get("total") if isinstance(raw, dict) else None
        if not isinstance(totals, dict):
            raise ExchangeError("exchange balance has no total map", code="bad_balance")
        balances: dict[str, Decimal] = {}
        for asset, value in totals.items():
            if value is None:
                continue
            balances[str(asset)] = _decimal(value)
        return balances


_STATUS = {
    "open": OrderStatus.OPEN,
    "closed": OrderStatus.CLOSED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
    "expired": OrderStatus.EXPIRED,
}


def _order_from_ccxt(
    raw: dict[str, Any], *, fallback_client_id: str | None = None
) -> ExchangeOrder:
    status_raw = str(raw.get("status") or "").lower()
    try:
        status = _STATUS[status_raw]
    except KeyError as exc:
        raise ExchangeError(
            f"unknown order status {status_raw!r}", code="unknown_order_status"
        ) from exc

    info = raw.get("info")
    raw_status = str(
        (info.get("status") if isinstance(info, dict) else None) or raw.get("status") or ""
    )

    filled = _optional_decimal(raw.get("filled")) or Decimal(0)
    amount = _optional_decimal(raw.get("amount"))
    if amount is None:
        amount = filled
    if amount is None or amount <= 0:
        raise ExchangeError("order has no positive quantity", code="bad_order")
    if not Decimal(0) <= filled <= amount:
        raise ExchangeError("order filled quantity is inconsistent", code="bad_order")

    average = _optional_decimal(raw.get("average")) or _optional_decimal(raw.get("price"))
    if average is None and filled > 0:
        cost = _optional_decimal(raw.get("cost"))
        if cost is not None:
            average = cost / filled
    if filled > 0 and (average is None or average <= 0):
        raise ExchangeError("filled order has no average price", code="unknown_order_fill")
    if filled == 0:
        average = None

    fee = Decimal(0)
    fee_raw = raw.get("fee")
    if isinstance(fee_raw, dict) and fee_raw.get("cost") is not None:
        fee = _decimal(fee_raw["cost"])
    elif isinstance(fee_raw, list):
        for item in fee_raw:
            if isinstance(item, dict) and item.get("cost") is not None:
                fee += _decimal(item["cost"])

    exchange_order_id = str(raw.get("id") or "")
    if not exchange_order_id:
        raise ExchangeError("order has no exchange id", code="bad_order")

    client_order_id = str(
        raw.get("clientOrderId")
        or (info.get("clientOrderId") if isinstance(info, dict) else None)
        or fallback_client_id
        or ""
    )
    if not client_order_id:
        raise ExchangeError("order has no client order id", code="bad_order")

    timestamp = raw.get("timestamp") or raw.get("lastTradeTimestamp") or utc_now_ms()

    return ExchangeOrder(
        client_order_id=client_order_id,
        exchange_order_id=exchange_order_id,
        symbol=str(raw.get("symbol") or ""),
        side=str(raw.get("side") or ""),
        status=status,
        quantity=amount,
        filled_quantity=filled,
        average_price=average,
        fee=fee,
        updated_at_ms=int(timestamp),
        raw_status=raw_status,
    )


def _map_error(exc: Exception, operation: str) -> ExchangeError:
    import ccxt

    if isinstance(exc, ccxt.RateLimitExceeded):
        code = "rate_limit"
    elif isinstance(exc, ccxt.NetworkError):
        code = "network_error"
    else:
        code = "exchange_rejected"
    return ExchangeError(f"{operation} failed: {_redact(str(exc))}", code=code)


def credentials_from_env() -> tuple[str, str]:
    """Read trade-only credentials. Never accept them as flags or config."""
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    secret = os.environ.get(API_SECRET_ENV, "").strip()
    if not api_key or not secret:
        raise CredentialError(
            f"missing exchange credentials: set {API_KEY_ENV} and {API_SECRET_ENV}"
        )
    return api_key, secret


def masked_key(api_key: str) -> str:
    """Identify a key without revealing more than its last four characters."""
    return f"***{api_key[-4:]}" if len(api_key) >= 4 else "***"


def build_adapter(config: ExecutionConfig, *, require_private: bool) -> CcxtAdapter:
    """Build the real adapter after the spot, clock and key-permission checks."""
    import ccxt

    options: dict[str, Any] = {
        "enableRateLimit": True,
        "options": {"defaultType": config.market_type},
    }
    if require_private:
        api_key, secret = credentials_from_env()
        options["apiKey"] = api_key
        options["apiSecret"] = secret

    exchange_class = getattr(ccxt, config.exchange, None)
    if exchange_class is None:
        raise ExchangeError(f"ccxt has no exchange {config.exchange!r}", code="unknown_exchange")
    adapter = CcxtAdapter(config.exchange, client=exchange_class(options))

    if adapter.market_type() != "spot":
        raise ExchangeError("exchange market type is not spot", code="non_spot_market")

    if require_private:
        permissions = adapter.fetch_permissions()
        if permissions & {"withdraw", "withdrawal"}:
            raise CredentialError("the exchange key can withdraw; use a trade-only key")
        server_time = adapter.server_time_ms()
        if server_time is not None:
            skew = abs(server_time - utc_now_ms())
            if skew > config.max_clock_skew_ms:
                raise ExchangeError(
                    f"local clock is {skew} ms away from the exchange; sync it first",
                    code="clock_skew",
                )
    return adapter
