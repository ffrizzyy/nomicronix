from decimal import Decimal

import ccxt
import pytest

from nomicronix.market_data.adapter import (
    AdapterError,
    CcxtAdapter,
    ExchangeAdapter,
    NetworkError,
    RateLimitError,
)
from tests.market_data.fakes import JAN_1_2025_MS, FakeExchangeAdapter, make_candles

HOUR_MS = 3_600_000


class StubCcxtClient:
    """Stands in for a ccxt exchange object: records calls, returns rows or raises."""

    def __init__(
        self, rows: list[list[float]] | None = None, error: Exception | None = None
    ) -> None:
        self.rows = rows or []
        self.error = error
        self.calls: list[tuple[str, str, int | None, int | None]] = []

    def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[list[float]]:
        self.calls.append((symbol, timeframe, since, limit))
        if self.error:
            raise self.error
        return self.rows


# --- FakeExchangeAdapter -------------------------------------------------------------------


def test_fake_returns_candles_from_since_in_ascending_order() -> None:
    candles = make_candles(JAN_1_2025_MS, 10)
    adapter: ExchangeAdapter = FakeExchangeAdapter(list(reversed(candles)))

    result = adapter.fetch_candles("BTC/USDT", "1h", JAN_1_2025_MS + 3 * HOUR_MS, 100)

    assert [c.open_time_ms for c in result] == [JAN_1_2025_MS + i * HOUR_MS for i in range(3, 10)]


def test_fake_honours_limit() -> None:
    adapter = FakeExchangeAdapter(make_candles(JAN_1_2025_MS, 10))

    result = adapter.fetch_candles("BTC/USDT", "1h", JAN_1_2025_MS, 4)

    assert len(result) == 4


def test_fake_filters_by_symbol_and_timeframe() -> None:
    adapter = FakeExchangeAdapter(
        make_candles(JAN_1_2025_MS, 3, symbol="BTC/USDT", timeframe="1h")
        + make_candles(JAN_1_2025_MS, 3, symbol="ETH/USDT", timeframe="1h")
        + make_candles(JAN_1_2025_MS, 3, symbol="BTC/USDT", timeframe="15m")
    )

    result = adapter.fetch_candles("ETH/USDT", "1h", JAN_1_2025_MS, 100)

    assert len(result) == 3
    assert {(c.symbol, c.timeframe) for c in result} == {("ETH/USDT", "1h")}


def test_fake_raises_scripted_rate_limit_then_recovers() -> None:
    adapter = FakeExchangeAdapter(make_candles(JAN_1_2025_MS, 2))
    adapter.fail_next(RateLimitError("slow down"))

    with pytest.raises(RateLimitError):
        adapter.fetch_candles("BTC/USDT", "1h", JAN_1_2025_MS, 10)

    assert len(adapter.fetch_candles("BTC/USDT", "1h", JAN_1_2025_MS, 10)) == 2


def test_fake_raises_scripted_disconnect_for_several_calls() -> None:
    adapter = FakeExchangeAdapter(make_candles(JAN_1_2025_MS, 2))
    adapter.fail_next(NetworkError("connection reset"), times=2)

    for _ in range(2):
        with pytest.raises(NetworkError):
            adapter.fetch_candles("BTC/USDT", "1h", JAN_1_2025_MS, 10)

    assert len(adapter.fetch_candles("BTC/USDT", "1h", JAN_1_2025_MS, 10)) == 2


def test_fake_can_return_a_duplicated_first_row() -> None:
    adapter = FakeExchangeAdapter(make_candles(JAN_1_2025_MS, 3), duplicate_first_row=True)

    result = adapter.fetch_candles("BTC/USDT", "1h", JAN_1_2025_MS, 10)

    assert [c.open_time_ms for c in result][:2] == [JAN_1_2025_MS, JAN_1_2025_MS]
    assert len(result) == 4


# --- CcxtAdapter (offline, with a stub client) ---------------------------------------------


def test_ccxt_adapter_converts_rows_to_candles_with_exact_decimals() -> None:
    client = StubCcxtClient(rows=[[JAN_1_2025_MS, 0.1, 0.3, 0.05, 0.2, 1.5]])
    adapter = CcxtAdapter("binance", client=client)

    (candle,) = adapter.fetch_candles("BTC/USDT", "1h", JAN_1_2025_MS, 1)

    assert candle.symbol == "BTC/USDT"
    assert candle.timeframe == "1h"
    assert candle.open_time_ms == JAN_1_2025_MS
    assert candle.open == Decimal("0.1")
    assert candle.high == Decimal("0.3")
    assert candle.low == Decimal("0.05")
    assert candle.close == Decimal("0.2")
    assert candle.volume == Decimal("1.5")


def test_ccxt_adapter_passes_since_and_limit_to_the_client() -> None:
    client = StubCcxtClient()
    adapter = CcxtAdapter("binance", client=client)

    adapter.fetch_candles("ETH/USDT", "15m", JAN_1_2025_MS, 500)

    assert client.calls == [("ETH/USDT", "15m", JAN_1_2025_MS, 500)]


def test_ccxt_adapter_returns_candles_sorted_ascending() -> None:
    client = StubCcxtClient(
        rows=[
            [JAN_1_2025_MS + HOUR_MS, 2, 3, 1, 2, 1],
            [JAN_1_2025_MS, 1, 2, 0.5, 1, 1],
        ]
    )
    adapter = CcxtAdapter("binance", client=client)

    result = adapter.fetch_candles("BTC/USDT", "1h", JAN_1_2025_MS, 10)

    assert [c.open_time_ms for c in result] == [JAN_1_2025_MS, JAN_1_2025_MS + HOUR_MS]


def test_ccxt_adapter_maps_rate_limit_errors() -> None:
    client = StubCcxtClient(error=ccxt.RateLimitExceeded("429"))
    adapter = CcxtAdapter("binance", client=client)

    with pytest.raises(RateLimitError):
        adapter.fetch_candles("BTC/USDT", "1h", JAN_1_2025_MS, 10)


def test_ccxt_adapter_maps_network_errors() -> None:
    client = StubCcxtClient(error=ccxt.NetworkError("timeout"))
    adapter = CcxtAdapter("binance", client=client)

    with pytest.raises(NetworkError):
        adapter.fetch_candles("BTC/USDT", "1h", JAN_1_2025_MS, 10)


def test_ccxt_adapter_rejects_unknown_timeframe_without_calling_the_exchange() -> None:
    client = StubCcxtClient()
    adapter = CcxtAdapter("binance", client=client)

    with pytest.raises(ValueError):
        adapter.fetch_candles("BTC/USDT", "2h", JAN_1_2025_MS, 10)

    assert client.calls == []


def test_ccxt_adapter_maps_other_exchange_errors_to_a_non_retryable_adapter_error() -> None:
    client = StubCcxtClient(error=ccxt.BaseError("binance does not have market symbol FOO/USDT"))
    adapter = CcxtAdapter("binance", client=client)

    with pytest.raises(AdapterError) as info:
        adapter.fetch_candles("FOO/USDT", "1h", JAN_1_2025_MS, 10)

    assert not isinstance(info.value, RateLimitError | NetworkError)
