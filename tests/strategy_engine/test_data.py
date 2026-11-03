import pytest

from nomicronix.market_data.models import Candle
from nomicronix.strategy_engine.data import (
    DataError,
    DataGapError,
    InsufficientDataError,
    load_candles,
)
from tests.strategy_engine.fakes import (
    HOUR_MS,
    JAN_1_2025_MS,
    make_candles,
    make_config,
    make_store,
)


def test_warmup_candles_are_loaded_before_the_start() -> None:
    store = make_store(make_candles(20))
    config = make_config(
        start_ms=JAN_1_2025_MS + 10 * HOUR_MS,
        end_ms=JAN_1_2025_MS + 20 * HOUR_MS,
        min_history=3,
    )

    candles = load_candles(store, config, warmup_candles=3)

    assert candles[0].open_time_ms == JAN_1_2025_MS + 7 * HOUR_MS
    assert candles[-1].open_time_ms == JAN_1_2025_MS + 19 * HOUR_MS
    assert len(candles) == 13


def test_a_gap_aborts_the_load() -> None:
    candles = make_candles(10)
    del candles[5]
    store = make_store(candles)
    config = make_config(end_ms=JAN_1_2025_MS + 10 * HOUR_MS)

    with pytest.raises(DataGapError) as caught:
        load_candles(store, config)
    assert caught.value.gaps == ((JAN_1_2025_MS + 5 * HOUR_MS, JAN_1_2025_MS + 5 * HOUR_MS),)


def test_too_few_candles_aborts_the_load() -> None:
    store = make_store(make_candles(5))
    config = make_config(min_history=10, end_ms=JAN_1_2025_MS + 5 * HOUR_MS)
    with pytest.raises(InsufficientDataError, match="need at least 10"):
        load_candles(store, config)


def test_unsorted_candles_are_rejected() -> None:
    candles = make_candles(5)
    shuffled = [candles[0], *reversed(candles[1:])]

    class UnorderedStore:
        def get_candles(
            self, symbol: str, timeframe: str, start_ms: int, end_ms: int
        ) -> list[Candle]:
            return shuffled

        def find_gaps(
            self, symbol: str, timeframe: str, start_ms: int, end_ms: int
        ) -> list[tuple[int, int]]:
            return []

    with pytest.raises(DataError, match="strictly ascending"):
        load_candles(UnorderedStore(), make_config())  # type: ignore[arg-type]


def test_negative_warmup_is_rejected() -> None:
    store = make_store(make_candles(5))
    with pytest.raises(ValueError, match="negative"):
        load_candles(store, make_config(), warmup_candles=-1)
