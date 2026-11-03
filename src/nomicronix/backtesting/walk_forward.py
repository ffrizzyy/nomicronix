"""Walk-forward parameter selection and out-of-sample stitching."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from itertools import product

from nomicronix.backtesting.data import load_candles
from nomicronix.backtesting.engine import run_backtest_on_candles
from nomicronix.backtesting.metrics import compute_metrics
from nomicronix.backtesting.models import (
    BacktestConfig,
    BacktestResult,
    EquityPoint,
    Metrics,
    Trade,
    WalkForwardConfig,
    WalkForwardResult,
    WindowResult,
)
from nomicronix.backtesting.strategy import StrategyFactory
from nomicronix.market_data.models import Candle, Timeframe
from nomicronix.market_data.store import MarketDataStore


class WalkForwardError(ValueError):
    """The requested data range cannot produce a complete walk-forward run."""


def run_walk_forward(
    store: MarketDataStore,
    factory: StrategyFactory,
    param_grid: Mapping[str, Sequence[object]],
    config: BacktestConfig,
    wf: WalkForwardConfig,
) -> WalkForwardResult:
    candles = load_candles(store, config)
    return run_walk_forward_on_candles(
        candles,
        factory,
        param_grid,
        config,
        wf,
    )


def run_walk_forward_on_candles(
    candles: list[Candle],
    factory: StrategyFactory,
    param_grid: Mapping[str, Sequence[object]],
    config: BacktestConfig,
    wf: WalkForwardConfig,
) -> WalkForwardResult:
    if not candles:
        raise WalkForwardError("no candles supplied")
    step_ms = Timeframe(config.timeframe).ms
    windows, bounds_warnings = _window_bounds(
        config.start_ms, config.end_ms, wf, step_ms, config.min_candles
    )
    if not windows:
        raise WalkForwardError("not enough candles for one train/test window")

    combos = _parameter_combinations(param_grid)
    oos_curve: list[EquityPoint] = []
    oos_trades: list[Trade] = []
    window_results: list[WindowResult] = []
    warnings: list[str] = list(bounds_warnings)
    oos_equity = config.initial_cash

    for train_start, train_end, test_start, test_end in windows:
        train_candles = _slice(
            candles,
            max(candles[0].open_time_ms, train_start - wf.warmup_candles * step_ms),
            train_end,
        )
        results = [
            (
                params,
                run_backtest_on_candles(
                    train_candles,
                    factory(params),
                    replace(
                        config,
                        start_ms=train_start,
                        end_ms=train_end,
                        warmup_candles=wf.warmup_candles,
                        initial_cash=config.initial_cash,
                    ),
                ),
            )
            for params in combos
        ]
        qualifying = [
            item for item in results if item[1].metrics.trade_count >= wf.min_train_trades
        ]
        window_warnings: list[str] = []
        if not qualifying:
            window_warnings.append("NO_QUALIFYING_PARAMS")
            qualifying = results
        params, train_result = max(qualifying, key=lambda item: _rank(item, wf.objective))

        test_candles = _slice(
            candles,
            max(candles[0].open_time_ms, test_start - wf.warmup_candles * step_ms),
            test_end,
        )
        test_result = run_backtest_on_candles(
            test_candles,
            factory(params),
            replace(
                config,
                start_ms=test_start,
                end_ms=test_end,
                warmup_candles=wf.warmup_candles,
                initial_cash=oos_equity,
            ),
        )
        if not test_result.equity_curve:
            raise WalkForwardError("test window produced no equity points")
        oos_curve.extend(test_result.equity_curve)
        oos_trades.extend(test_result.trades)
        oos_equity = test_result.equity_curve[-1].equity

        window_results.append(
            WindowResult(
                train_start_ms=train_start,
                train_end_ms=train_end,
                test_start_ms=test_start,
                test_end_ms=test_end,
                params=params,
                train=train_result,
                test=test_result,
                warnings=tuple(window_warnings),
            )
        )
        index = len(window_results) - 1
        warnings.extend(f"window {index}: {warning}" for warning in window_warnings)

    if not window_results:
        raise WalkForwardError("no walk-forward window completed")

    metrics = compute_metrics(
        oos_trades,
        oos_curve,
        config.initial_cash,
        config.timeframe,
    )
    return WalkForwardResult(
        config=config,
        walk_forward=wf,
        param_grid={key: tuple(values) for key, values in param_grid.items()},
        windows=tuple(window_results),
        oos_equity_curve=tuple(oos_curve),
        oos_trades=tuple(oos_trades),
        metrics=metrics,
        in_sample_mean=_mean_metrics([window.train.metrics for window in window_results]),
        warnings=tuple(warnings),
    )


def _window_bounds(
    start_ms: int,
    end_ms: int,
    wf: WalkForwardConfig,
    step_ms: int,
    min_candles: int,
) -> tuple[list[tuple[int, int, int, int]], list[str]]:
    if wf.step_ms < wf.test_ms:
        raise WalkForwardError("step must be at least the test window to avoid overlap")
    windows: list[tuple[int, int, int, int]] = []
    warnings: list[str] = []
    t = start_ms
    while t + wf.train_ms + wf.test_ms <= end_ms:
        windows.append((t, t + wf.train_ms, t + wf.train_ms, t + wf.train_ms + wf.test_ms))
        t += wf.step_ms
    if t + wf.train_ms < end_ms:
        test_start = t + wf.train_ms
        remaining_ms = end_ms - test_start
        if wf.allow_partial and remaining_ms >= min_candles * step_ms:
            windows.append((t, test_start, test_start, end_ms))
        else:
            reason = "PARTIAL_WINDOW_TOO_SHORT" if wf.allow_partial else "PARTIAL_WINDOW_DROPPED"
            warnings.append(f"{reason} at {test_start}")
    return windows, warnings


def _parameter_combinations(
    param_grid: Mapping[str, Sequence[object]],
) -> list[dict[str, object]]:
    keys = sorted(param_grid)
    if not keys:
        return [{}]
    return [
        dict(zip(keys, values, strict=True))
        for values in product(*(param_grid[key] for key in keys))
    ]


def _rank(
    item: tuple[dict[str, object], BacktestResult],
    objective: str,
) -> tuple[float, float, int, str]:
    params, result = item
    value = _objective_value(result.metrics, objective)
    total = result.metrics.total_return
    return (
        value if value is not None else float("-inf"),
        total if total is not None else float("-inf"),
        -result.metrics.trade_count,
        json.dumps(params, sort_keys=True, default=str),
    )


def _objective_value(metrics: Metrics, objective: str) -> float | None:
    if objective == "sharpe":
        return metrics.sharpe
    if objective == "total_return":
        return metrics.total_return
    return metrics.calmar


def _slice(candles: list[Candle], start_ms: int, end_ms: int) -> list[Candle]:
    return [c for c in candles if start_ms <= c.open_time_ms < end_ms]


def _mean_metrics(metrics: list[Metrics]) -> dict[str, float | None]:
    return {
        "total_return": _mean([m.total_return for m in metrics]),
        "sharpe": _mean([m.sharpe for m in metrics]),
        "calmar": _mean([m.calmar for m in metrics]),
    }


def _mean(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None
