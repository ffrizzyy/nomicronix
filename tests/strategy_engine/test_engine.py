from decimal import Decimal

import pytest

from nomicronix.strategy_engine.engine import (
    ScanError,
    SignalLimitError,
    scan,
    scan_candles,
    signal_id,
)
from nomicronix.strategy_engine.models import ScanConfig
from tests.strategy_engine.fakes import (
    HOUR_MS,
    JAN_1_2025_MS,
    AlwaysFlat,
    ExplodingProposer,
    FixedProposer,
    RejectingProposer,
    SpyProposer,
    make_candles,
    make_config,
    make_store,
)


def test_fixed_proposer_emits_inside_the_window_with_provenance() -> None:
    candles = make_candles(10)
    config = make_config(
        start_ms=JAN_1_2025_MS + 3 * HOUR_MS,
        end_ms=JAN_1_2025_MS + 10 * HOUR_MS,
        min_history=2,
    )

    batch = scan_candles(candles, FixedProposer(), config)

    assert len(batch.signals) == 7
    first = batch.signals[0]
    assert first.signal_id == signal_id("fixed", "BTC/USDT", "1h", JAN_1_2025_MS + 3 * HOUR_MS)
    assert first.candle_open_time_ms == JAN_1_2025_MS + 3 * HOUR_MS
    assert first.created_at_ms == JAN_1_2025_MS + 4 * HOUR_MS
    assert first.expires_at_ms == first.created_at_ms + 300_000
    assert first.side.value == "buy"
    assert batch.strategy_id == "fixed"
    assert (batch.first_candle_ms, batch.last_candle_ms, batch.candle_count) == (
        JAN_1_2025_MS,
        JAN_1_2025_MS + 9 * HOUR_MS,
        10,
    )
    assert batch.warnings == ()


def test_signals_stop_at_the_half_open_end() -> None:
    candles = make_candles(10)
    config = make_config(
        start_ms=JAN_1_2025_MS + 3 * HOUR_MS,
        end_ms=JAN_1_2025_MS + 5 * HOUR_MS,
        min_history=2,
    )
    batch = scan_candles(candles, FixedProposer(), config)
    assert [signal.candle_open_time_ms for signal in batch.signals] == [
        JAN_1_2025_MS + 3 * HOUR_MS,
        JAN_1_2025_MS + 4 * HOUR_MS,
    ]


def test_confidence_below_the_floor_is_a_warning_not_a_signal() -> None:
    candles = make_candles(5)
    config = make_config(min_confidence=0.7)
    batch = scan_candles(candles, FixedProposer(confidence=0.6), config)

    assert batch.signals == ()
    assert len(batch.warnings) == 5
    assert "confidence 0.600 below 0.700" in batch.warnings[0]


def test_rejecting_proposer_is_reported_and_the_scan_continues() -> None:
    batch = scan_candles(make_candles(3), RejectingProposer(), make_config())
    assert batch.signals == ()
    assert len(batch.warnings) == 3
    assert "bad idea from test" in batch.warnings[0]


def test_unexpected_proposer_errors_abort_the_scan() -> None:
    with pytest.raises(RuntimeError, match="boom"):
        scan_candles(make_candles(3), ExplodingProposer(), make_config())


def test_max_signals_is_an_error_not_a_truncated_batch() -> None:
    config = make_config(max_signals=2)
    with pytest.raises(SignalLimitError):
        scan_candles(make_candles(5), FixedProposer(), config)


def test_always_flat_produces_an_empty_batch() -> None:
    batch = scan_candles(make_candles(5), AlwaysFlat(), make_config())
    assert batch.signals == ()
    assert batch.warnings == ()


def test_the_spy_never_sees_a_later_candle() -> None:
    candles = make_candles(10)
    spy = SpyProposer()
    config = make_config(
        start_ms=JAN_1_2025_MS + 3 * HOUR_MS,
        end_ms=JAN_1_2025_MS + 10 * HOUR_MS,
        min_history=2,
    )

    scan_candles(candles, spy, config)

    assert spy.seen_lengths == [4, 5, 6, 7, 8, 9, 10]
    assert spy.seen_times == [JAN_1_2025_MS + index * HOUR_MS for index in range(3, 10)]
    assert all(length == index + 1 for index, length in enumerate(spy.seen_lengths, start=3))


def test_proposer_min_history_is_respected_even_below_config() -> None:
    candles = make_candles(10)
    spy = SpyProposer(min_history=8)
    config = make_config(min_history=1)

    scan_candles(candles, spy, config)

    assert spy.seen_lengths == [8, 9, 10]


def test_unsorted_candles_are_rejected() -> None:
    candles = make_candles(3)
    shuffled = [candles[0], candles[2], candles[1]]
    with pytest.raises(ValueError, match="strictly ascending"):
        scan_candles(shuffled, FixedProposer(), make_config())


def test_symbol_mismatch_is_rejected() -> None:
    candles = make_candles(3)
    config = make_config(symbol="ETH/USDT")
    with pytest.raises(ScanError, match="does not match"):
        scan_candles(candles, FixedProposer(), config)


def test_too_few_candles_are_rejected() -> None:
    candles = make_candles(3)
    config = make_config(min_history=5)
    with pytest.raises(ValueError, match="need at least 5 candles"):
        scan_candles(candles, FixedProposer(), config)


def test_empty_window_is_rejected() -> None:
    candles = make_candles(3)
    config = make_config(
        start_ms=JAN_1_2025_MS + 10 * HOUR_MS,
        end_ms=JAN_1_2025_MS + 12 * HOUR_MS,
    )
    with pytest.raises(ValueError, match="no candles inside"):
        scan_candles(candles, FixedProposer(), config)


def test_empty_candle_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="no candles supplied"):
        scan_candles([], FixedProposer(), make_config())


def test_scan_loads_from_the_store() -> None:
    store = make_store(make_candles(10))
    config = make_config(
        start_ms=JAN_1_2025_MS,
        end_ms=JAN_1_2025_MS + 10 * HOUR_MS,
        min_history=2,
    )
    batch = scan(store, FixedProposer(), config)
    assert len(batch.signals) == 9
    assert batch.signals[0].candle_open_time_ms == JAN_1_2025_MS + HOUR_MS
    assert batch.signals[0].features == {"atr": "2.5"}
    assert isinstance(batch.signals[0].entry_price, Decimal)


def test_scan_config_is_exposed_on_the_batch() -> None:
    config = ScanConfig(
        symbol="BTC/USDT",
        timeframe="1h",
        start_ms=JAN_1_2025_MS,
        end_ms=JAN_1_2025_MS + HOUR_MS,
        min_history=1,
    )
    batch = scan_candles(make_candles(2), FixedProposer(), config)
    assert batch.config is config
