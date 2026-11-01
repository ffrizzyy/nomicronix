"""Exchange access for market data.

Public endpoints only: this module never takes or needs API keys.
"""

from decimal import Decimal
from typing import Any, Protocol

from nomicronix.market_data.models import Candle, Timeframe


class AdapterError(Exception):
    """Base class for errors raised by an exchange adapter."""


class RateLimitError(AdapterError):
    """The exchange asked us to slow down. Safe to retry after a backoff."""


class NetworkError(AdapterError):
    """Disconnect, timeout or exchange outage. Safe to retry after a backoff."""


class ExchangeAdapter(Protocol):
    name: str

    def fetch_candles(
        self, symbol: str, timeframe: str, since_ms: int, limit: int
    ) -> list[Candle]:
        """Candles with open_time_ms >= since_ms, ascending, at most `limit`.

        The last candle may still be in progress: callers must drop it before storing.
        """
        ...


class CcxtAdapter:
    """ExchangeAdapter backed by ccxt, using public endpoints only."""

    def __init__(self, exchange_id: str = "binance", client: Any = None) -> None:
        self.name = exchange_id
        if client is None:
            import ccxt

            client = getattr(ccxt, exchange_id)({"enableRateLimit": True})
        self._client = client

    def fetch_candles(
        self, symbol: str, timeframe: str, since_ms: int, limit: int
    ) -> list[Candle]:
        import ccxt

        Timeframe(timeframe)  # raises ValueError for unsupported timeframes
        try:
            rows = self._client.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=limit)
        except ccxt.RateLimitExceeded as exc:
            raise RateLimitError(str(exc)) from exc
        except ccxt.NetworkError as exc:
            raise NetworkError(str(exc)) from exc
        except ccxt.BaseError as exc:  # e.g. unknown symbol: retrying will not help
            raise AdapterError(str(exc)) from exc
        candles = [_row_to_candle(symbol, timeframe, row) for row in rows]
        return sorted(candles, key=lambda c: c.open_time_ms)


def _row_to_candle(symbol: str, timeframe: str, row: list[Any]) -> Candle:
    open_time, open_, high, low, close, volume = row[:6]
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        open_time_ms=int(open_time),
        open=_to_decimal(open_),
        high=_to_decimal(high),
        low=_to_decimal(low),
        close=_to_decimal(close),
        volume=_to_decimal(volume),
    )


def _to_decimal(value: Any) -> Decimal:
    # str() first: Decimal(0.1) would carry binary float noise, Decimal("0.1") is exact.
    return Decimal(str(value))
