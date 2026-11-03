from decimal import Decimal

import pytest

from nomicronix.backtesting.data import (
    DataError,
    DataGapError,
    InsufficientDataError,
    load_candles,
)
from nomicronix.backtesting.models import (
    BacktestResult,
    EquityPoint,
    Metrics,
    WalkForwardConfig,
)
from nomicronix.market_data.models import Candle
from tests.backtesting.fakes import (
    HOUR_MS,
    JAN_1_2025_MS,
    make_candles,
    make_config,
    make_store,
)


def test_config_validates_bounds() -> None:
    with pytest.raises(ValueError):
        make_config(start_ms=2, end_ms=1)
    with pytest.raises(ValueError):
        make_config(initial_cash=Decimal("0"))
    with pytest.raises(ValueError):
        make_config(fee_bps=Decimal("-1"))
    with pytest.raises(ValueError):
        make_config(position_fraction=Decimal("1.1"))
    with pytest.raises(ValueError):
        make_config(min_candles=0)
    with pytest.raises(ValueError):
        make_config(warmup_candles=-1)
    with pytest.raises(ValueError):
        make_config(quantity_step=Decimal("0"))


def test_walk_forward_config_validates_bounds() -> None:
    with pytest.raises(ValueError):
        WalkForwardConfig(train_ms=0, test_ms=1, step_ms=1)
    with pytest.raises(ValueError):
        WalkForwardConfig(train_ms=1, test_ms=1, step_ms=1, objective="magic")
    with pytest.raises(ValueError):
        WalkForwardConfig(train_ms=1, test_ms=1, step_ms=1, min_train_trades=-1)


def test_load_candles_returns_the_requested_range() -> None:
    candles = make_candles(100)
    store = make_store(candles)
    config = make_config(start_ms=JAN_1_2025_MS, end_ms=JAN_1_2025_MS + 50 * HOUR_MS)
    loaded = load_candles(store, config)
    assert len(loaded) == 50
    assert loaded[0].open_time_ms == JAN_1_2025_MS
    assert loaded[-1].open_time_ms == JAN_1_2025_MS + 49 * HOUR_MS


def test_warmup_candles_are_loaded_before_start() -> None:
    candles = make_candles(100)
    store = make_store(candles)
    config = make_config(
        start_ms=JAN_1_2025_MS + 20 * HOUR_MS,
        end_ms=JAN_1_2025_MS + 50 * HOUR_MS,
        warmup_candles=5,
    )
    loaded = load_candles(store, config)
    assert loaded[0].open_time_ms == JAN_1_2025_MS + 15 * HOUR_MS


def test_gap_in_range_is_a_hard_error() -> None:
    candles = make_candles(100)
    del candles[10]
    store = make_store(candles)
    config = make_config(start_ms=JAN_1_2025_MS, end_ms=JAN_1_2025_MS + 50 * HOUR_MS)
    with pytest.raises(DataGapError) as caught:
        load_candles(store, config)
    assert caught.value.gaps


def test_insufficient_candles_is_an_error() -> None:
    store = make_store(make_candles(5))
    config = make_config(
        start_ms=JAN_1_2025_MS,
        end_ms=JAN_1_2025_MS + 5 * HOUR_MS,
        min_candles=10,
    )
    with pytest.raises(InsufficientDataError):
        load_candles(store, config)


def test_unsorted_candles_are_rejected() -> None:
    candles = make_candles(10)
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

    config = make_config(
        start_ms=JAN_1_2025_MS,
        end_ms=JAN_1_2025_MS + 10 * HOUR_MS,
    )
    with pytest.raises(DataError):
        load_candles(UnorderedStore(), config)  # type: ignore[arg-type]


def test_result_models_are_dataclasses() -> None:
    metrics = Metrics(
        total_return=None,
        cagr=None,
        max_drawdown=None,
        sharpe=None,
        sortino=None,
        calmar=None,
        win_rate=None,
        profit_factor=None,
        expectancy=None,
        average_win=None,
        average_loss=None,
        fees_paid=Decimal(0),
        turnover=Decimal(0),
        exposure=None,
        trade_count=0,
    )
    assert metrics.trade_count == 0
    point = EquityPoint(JAN_1_2025_MS, Decimal("1000"), False)
    assert point.equity == Decimal("1000")
    assert BacktestResult.__name__ == "BacktestResult"
