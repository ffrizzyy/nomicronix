from collections.abc import Mapping
from decimal import Decimal

import pytest

from nomicronix.backtesting.models import BacktestConfig, WalkForwardConfig
from nomicronix.backtesting.strategy import Strategy
from nomicronix.backtesting.walk_forward import (
    WalkForwardError,
    run_walk_forward,
    run_walk_forward_on_candles,
)
from tests.backtesting.fakes import (
    HOUR_MS,
    JAN_1_2025_MS,
    BiasEntry,
    make_candles,
    make_config,
    make_store,
)


def wf(**overrides: object) -> WalkForwardConfig:
    defaults: dict[str, object] = {
        "train_ms": 10 * HOUR_MS,
        "test_ms": 5 * HOUR_MS,
        "step_ms": 5 * HOUR_MS,
        "objective": "total_return",
        "min_train_trades": 1,
        "warmup_candles": 0,
        "allow_partial": False,
    }
    defaults.update(overrides)
    return WalkForwardConfig(**defaults)  # type: ignore[arg-type]


def factory(params: Mapping[str, object]) -> Strategy:
    return BiasEntry(**params)  # type: ignore[arg-type]


def config(hours: int = 60) -> BacktestConfig:
    return make_config(
        start_ms=JAN_1_2025_MS,
        end_ms=JAN_1_2025_MS + hours * HOUR_MS,
    )


def test_windows_are_complete_and_never_overlap() -> None:
    result = run_walk_forward_on_candles(
        make_candles(60),
        factory,
        {"bias": (0, 1)},
        config(),
        wf(),
    )

    assert len(result.windows) == 10
    for current, following in zip(result.windows, result.windows[1:], strict=False):
        assert current.test_end_ms <= following.test_start_ms
        assert current.train_end_ms == current.test_start_ms


def test_step_larger_than_test_keeps_windows_disjoint() -> None:
    result = run_walk_forward_on_candles(
        make_candles(60),
        factory,
        {"bias": (1,)},
        config(),
        wf(step_ms=7 * HOUR_MS),
    )
    for current, following in zip(result.windows, result.windows[1:], strict=False):
        assert current.test_end_ms <= following.test_start_ms


def test_selection_keeps_the_parameter_that_traded_in_training_only() -> None:
    result = run_walk_forward_on_candles(
        make_candles(60),
        factory,
        {"bias": (0, 1)},
        config(),
        wf(),
    )

    assert {window.params["bias"] for window in result.windows} == {1}
    assert all(window.test.metrics.trade_count >= 1 for window in result.windows)
    assert all(window.train.metrics.trade_count >= 1 for window in result.windows)
    assert len(result.oos_trades) == len(result.windows)


def test_grid_is_expanded_deterministically() -> None:
    result = run_walk_forward_on_candles(
        make_candles(60),
        factory,
        {"stop_pct": (0.05, 0.10), "bias": (1,)},
        config(),
        wf(),
    )
    assert result.param_grid == {"stop_pct": (0.05, 0.10), "bias": (1,)}
    assert all(set(window.params) == {"bias", "stop_pct"} for window in result.windows)


def test_oos_equity_is_compounded_across_windows() -> None:
    result = run_walk_forward_on_candles(
        make_candles(60),
        factory,
        {"bias": (1,)},
        config(),
        wf(),
    )

    assert len(result.oos_equity_curve) == 50
    assert result.oos_equity_curve[0].equity == Decimal("1000")
    assert result.oos_equity_curve[-1].equity > Decimal("1000")
    assert result.metrics.trade_count == len(result.oos_trades)
    assert result.metrics.total_return == pytest.approx(
        float(result.oos_equity_curve[-1].equity / Decimal("1000") - 1)
    )


def test_no_qualifying_params_is_reported_not_hidden() -> None:
    result = run_walk_forward_on_candles(
        make_candles(60),
        factory,
        {"bias": (0,)},
        config(),
        wf(min_train_trades=5),
    )

    assert result.windows[0].warnings == ("NO_QUALIFYING_PARAMS",)
    assert result.warnings[0] == "window 0: NO_QUALIFYING_PARAMS"
    assert result.windows[0].params == {"bias": 0}
    assert result.metrics.trade_count == 0


def test_overlapping_step_is_rejected() -> None:
    with pytest.raises(WalkForwardError):
        run_walk_forward_on_candles(
            make_candles(60),
            factory,
            {"bias": (1,)},
            config(),
            wf(step_ms=1 * HOUR_MS),
        )


def test_range_too_short_for_one_window_is_rejected() -> None:
    with pytest.raises(WalkForwardError):
        run_walk_forward_on_candles(
            make_candles(12),
            factory,
            {"bias": (1,)},
            config(12),
            wf(),
        )


def test_empty_candles_are_rejected() -> None:
    with pytest.raises(WalkForwardError):
        run_walk_forward_on_candles([], factory, {"bias": (1,)}, config(), wf())


def test_partial_window_is_opt_in() -> None:
    complete = run_walk_forward_on_candles(
        make_candles(22),
        factory,
        {"bias": (1,)},
        config(22),
        wf(),
    )
    partial = run_walk_forward_on_candles(
        make_candles(22),
        factory,
        {"bias": (1,)},
        config(22),
        wf(allow_partial=True),
    )

    assert len(complete.windows) == 2
    assert complete.warnings == (f"PARTIAL_WINDOW_DROPPED at {JAN_1_2025_MS + 20 * HOUR_MS}",)
    assert len(partial.windows) == 3
    assert partial.warnings == ()
    assert partial.windows[-1].test_end_ms == config(22).end_ms


def test_partial_window_too_short_is_reported() -> None:
    result = run_walk_forward_on_candles(
        make_candles(21),
        factory,
        {"bias": (1,)},
        make_config(
            start_ms=JAN_1_2025_MS,
            end_ms=JAN_1_2025_MS + 20 * HOUR_MS + 30 * 60_000,
        ),
        wf(allow_partial=True),
    )

    assert len(result.windows) == 2
    assert result.warnings == (f"PARTIAL_WINDOW_TOO_SHORT at {JAN_1_2025_MS + 20 * HOUR_MS}",)


def test_run_walk_forward_loads_from_the_store() -> None:
    store = make_store(make_candles(60))
    result = run_walk_forward(
        store,
        factory,
        {"bias": (1,)},
        config(),
        wf(),
    )
    assert result.windows
    assert result.in_sample_mean["total_return"] is not None
    assert result.oos_trades


def test_repeated_runs_are_identical() -> None:
    first = run_walk_forward_on_candles(make_candles(60), factory, {"bias": (0, 1)}, config(), wf())
    second = run_walk_forward_on_candles(
        make_candles(60), factory, {"bias": (0, 1)}, config(), wf()
    )
    assert first.windows == second.windows
    assert first.oos_equity_curve == second.oos_equity_curve
    assert first.metrics == second.metrics
