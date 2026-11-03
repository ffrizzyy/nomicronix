from decimal import Decimal

import pytest

from nomicronix.backtesting.engine import run_backtest, run_backtest_on_candles
from nomicronix.backtesting.models import Action, ExitReason, Signal, SkipReason
from nomicronix.backtesting.strategy import HistoryView
from tests.backtesting.fakes import (
    HOUR_MS,
    JAN_1_2025_MS,
    AlwaysEntry,
    EntryOnce,
    ScriptedStrategy,
    SpyStrategy,
    make_candles,
    make_config,
    make_store,
)


def test_entry_and_exit_fill_at_the_next_open() -> None:
    candles = make_candles(20)
    strategy = ScriptedStrategy(
        {
            0: Signal(
                Action.ENTER_LONG,
                stop_price=Decimal("90"),
                target_price=Decimal("120"),
            ),
            2: Signal(Action.EXIT_LONG),
        }
    )
    config = make_config(
        start_ms=JAN_1_2025_MS,
        end_ms=JAN_1_2025_MS + 20 * HOUR_MS,
    )
    result = run_backtest_on_candles(candles, strategy, config)

    trade = result.trades[0]
    assert trade.entry_time_ms == candles[1].open_time_ms
    assert trade.entry_price == candles[1].open
    assert trade.exit_time_ms == candles[3].open_time_ms
    assert trade.exit_price == candles[3].open
    assert trade.exit_reason is ExitReason.SIGNAL
    assert result.candle_count == 20


def test_history_view_is_strictly_causal() -> None:
    candles = make_candles(30)
    strategy = SpyStrategy()
    config = make_config(
        start_ms=JAN_1_2025_MS,
        end_ms=JAN_1_2025_MS + 30 * HOUR_MS,
    )
    run_backtest_on_candles(candles, strategy, config)
    assert strategy.violations == []


def test_warmup_signals_are_suppressed() -> None:
    candles = make_candles(30)
    strategy = EntryOnce()
    config = make_config(
        start_ms=JAN_1_2025_MS + 10 * HOUR_MS,
        end_ms=JAN_1_2025_MS + 30 * HOUR_MS,
        warmup_candles=5,
    )
    result = run_backtest_on_candles(candles, strategy, config)
    assert result.trades == ()
    assert result.candle_count == 20


def test_invalid_entry_signal_is_recorded() -> None:
    candles = make_candles(10)
    strategy = ScriptedStrategy(
        {
            0: Signal(
                Action.ENTER_LONG,
                stop_price=Decimal("999"),
                target_price=Decimal("1000"),
            )
        }
    )
    config = make_config(
        start_ms=JAN_1_2025_MS,
        end_ms=JAN_1_2025_MS + 10 * HOUR_MS,
    )
    result = run_backtest_on_candles(candles, strategy, config)
    assert any(event.reason == SkipReason.INVALID_SIGNAL.value for event in result.events)


def test_repeated_entry_signals_are_ignored_while_long() -> None:
    candles = make_candles(20)
    strategy = AlwaysEntry()
    config = make_config(
        start_ms=JAN_1_2025_MS,
        end_ms=JAN_1_2025_MS + 20 * HOUR_MS,
    )
    result = run_backtest_on_candles(candles, strategy, config)
    assert any(event.reason == SkipReason.IGNORED_ENTRY.value for event in result.events)


def test_signal_on_the_last_candle_is_pending_at_end() -> None:
    candles = make_candles(10)
    strategy = ScriptedStrategy(
        {
            9: Signal(
                Action.ENTER_LONG,
                stop_price=Decimal("90"),
                target_price=Decimal("120"),
            )
        }
    )
    config = make_config(
        start_ms=JAN_1_2025_MS,
        end_ms=JAN_1_2025_MS + 10 * HOUR_MS,
    )
    result = run_backtest_on_candles(candles, strategy, config)
    assert any(event.reason == SkipReason.END_OF_DATA_PENDING.value for event in result.events)


def test_open_position_is_forced_closed_at_the_end() -> None:
    candles = make_candles(10)
    config = make_config(
        start_ms=JAN_1_2025_MS,
        end_ms=JAN_1_2025_MS + 10 * HOUR_MS,
    )
    result = run_backtest_on_candles(candles, EntryOnce(), config)
    assert result.trades[0].exit_reason is ExitReason.END_OF_DATA
    assert result.equity_curve[-1].exposed is False


def test_run_backtest_reads_from_the_store() -> None:
    candles = make_candles(30)
    store = make_store(candles)
    config = make_config(
        start_ms=JAN_1_2025_MS,
        end_ms=JAN_1_2025_MS + 30 * HOUR_MS,
    )
    result = run_backtest(store, EntryOnce(), config)
    assert result.candle_count == 30
    assert result.trades


def test_empty_candles_raise() -> None:
    with pytest.raises(ValueError):
        run_backtest_on_candles([], EntryOnce(), make_config())


def test_history_view_supports_negative_index_and_slice() -> None:
    candles = make_candles(5)
    view = HistoryView(candles, 3)
    assert len(view) == 3
    assert view[-1] == candles[2]
    assert view[0:2] == (candles[0], candles[1])
    with pytest.raises(IndexError):
        _ = view[3]


def test_history_view_rejects_invalid_end() -> None:
    with pytest.raises(ValueError):
        HistoryView(make_candles(2), 3)
