from decimal import Decimal
from pathlib import Path

import pytest

from nomicronix.market_data.models import Candle
from nomicronix.market_data.store import OpenCandleError, SqliteStore
from tests.market_data.fakes import JAN_1_2025_MS, make_candle, make_candles

HOUR_MS = 3_600_000
NOW_MS = JAN_1_2025_MS + 100 * HOUR_MS


def make_store(exchange: str = "binance") -> SqliteStore:
    return SqliteStore(":memory:", exchange, now_ms=lambda: NOW_MS)


def open_times(candles: list[Candle]) -> list[int]:
    return [c.open_time_ms for c in candles]


# --- writing and reading --------------------------------------------------------------------


def test_stored_candle_comes_back_with_exact_decimals() -> None:
    store = make_store()
    candle = make_candle(JAN_1_2025_MS, price="93500.10")

    store.upsert_candles([candle])

    (loaded,) = store.get_candles("BTC/USDT", "1h", JAN_1_2025_MS, JAN_1_2025_MS + HOUR_MS)
    assert loaded == candle
    assert str(loaded.open) == "93500.10"
    assert isinstance(loaded.volume, Decimal)


def test_get_candles_returns_ascending_order_even_when_written_out_of_order() -> None:
    store = make_store()
    candles = make_candles(JAN_1_2025_MS, 5)

    store.upsert_candles([candles[3], candles[0], candles[4], candles[1], candles[2]])

    result = store.get_candles("BTC/USDT", "1h", JAN_1_2025_MS, JAN_1_2025_MS + 5 * HOUR_MS)
    assert open_times(result) == open_times(candles)


def test_upserting_the_same_candle_twice_keeps_one_row() -> None:
    store = make_store()
    candle = make_candle(JAN_1_2025_MS)

    store.upsert_candles([candle])
    store.upsert_candles([candle])

    result = store.get_candles("BTC/USDT", "1h", JAN_1_2025_MS, JAN_1_2025_MS + HOUR_MS)
    assert len(result) == 1


def test_upsert_replaces_an_existing_candle_with_new_values() -> None:
    store = make_store()
    store.upsert_candles([make_candle(JAN_1_2025_MS, price="100")])

    store.upsert_candles([make_candle(JAN_1_2025_MS, price="250")])

    (loaded,) = store.get_candles("BTC/USDT", "1h", JAN_1_2025_MS, JAN_1_2025_MS + HOUR_MS)
    assert loaded.open == Decimal("250")


def test_get_candles_range_includes_start_and_excludes_end() -> None:
    store = make_store()
    store.upsert_candles(make_candles(JAN_1_2025_MS, 5))

    result = store.get_candles(
        "BTC/USDT", "1h", JAN_1_2025_MS + 1 * HOUR_MS, JAN_1_2025_MS + 4 * HOUR_MS
    )

    assert open_times(result) == [
        JAN_1_2025_MS + 1 * HOUR_MS,
        JAN_1_2025_MS + 2 * HOUR_MS,
        JAN_1_2025_MS + 3 * HOUR_MS,
    ]


def test_get_candles_is_scoped_by_symbol_and_timeframe() -> None:
    store = make_store()
    store.upsert_candles(make_candles(JAN_1_2025_MS, 3, symbol="BTC/USDT", timeframe="1h"))
    store.upsert_candles(make_candles(JAN_1_2025_MS, 2, symbol="ETH/USDT", timeframe="1h"))
    store.upsert_candles(make_candles(JAN_1_2025_MS, 4, symbol="BTC/USDT", timeframe="15m"))

    result = store.get_candles("ETH/USDT", "1h", JAN_1_2025_MS, JAN_1_2025_MS + 10 * HOUR_MS)

    assert len(result) == 2
    assert {(c.symbol, c.timeframe) for c in result} == {("ETH/USDT", "1h")}


# --- latest_candle --------------------------------------------------------------------------


def test_latest_candle_returns_the_newest() -> None:
    store = make_store()
    candles = make_candles(JAN_1_2025_MS, 5)
    store.upsert_candles(candles)

    assert store.latest_candle("BTC/USDT", "1h") == candles[-1]


def test_latest_candle_is_none_when_nothing_is_stored_for_that_series() -> None:
    store = make_store()
    store.upsert_candles(make_candles(JAN_1_2025_MS, 3, symbol="ETH/USDT"))

    assert store.latest_candle("BTC/USDT", "1h") is None


# --- closed candles only --------------------------------------------------------------------


def test_upsert_rejects_a_candle_that_has_not_closed() -> None:
    store = make_store()
    still_open = make_candle(NOW_MS)

    with pytest.raises(OpenCandleError):
        store.upsert_candles([still_open])


def test_a_rejected_batch_writes_nothing() -> None:
    store = make_store()
    closed = make_candle(NOW_MS - 5 * HOUR_MS)
    still_open = make_candle(NOW_MS)

    with pytest.raises(OpenCandleError):
        store.upsert_candles([closed, still_open])

    assert store.latest_candle("BTC/USDT", "1h") is None


def test_a_candle_closing_exactly_now_is_accepted() -> None:
    store = make_store()
    just_closed = make_candle(NOW_MS - HOUR_MS)

    store.upsert_candles([just_closed])

    assert store.latest_candle("BTC/USDT", "1h") == just_closed


def test_upsert_rejects_an_unknown_timeframe() -> None:
    store = make_store()

    with pytest.raises(ValueError):
        store.upsert_candles([make_candle(JAN_1_2025_MS, timeframe="2h")])


# --- persistence and isolation --------------------------------------------------------------


def test_data_survives_reopening_the_database_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "market.sqlite"
    first = SqliteStore(path, "binance", now_ms=lambda: NOW_MS)
    first.upsert_candles(make_candles(JAN_1_2025_MS, 3))
    first.close()

    second = SqliteStore(path, "binance", now_ms=lambda: NOW_MS)

    result = second.get_candles("BTC/USDT", "1h", JAN_1_2025_MS, JAN_1_2025_MS + 10 * HOUR_MS)
    assert len(result) == 3


def test_exchanges_do_not_see_each_others_candles(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    binance = SqliteStore(path, "binance", now_ms=lambda: NOW_MS)
    bybit = SqliteStore(path, "bybit", now_ms=lambda: NOW_MS)

    binance.upsert_candles(make_candles(JAN_1_2025_MS, 3))

    assert bybit.latest_candle("BTC/USDT", "1h") is None
    assert binance.latest_candle("BTC/USDT", "1h") is not None
