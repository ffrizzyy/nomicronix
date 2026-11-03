import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

import pytest

from nomicronix.backtesting.engine import run_backtest_on_candles
from nomicronix.backtesting.models import (
    BacktestResult,
    WalkForwardConfig,
    WalkForwardResult,
)
from nomicronix.backtesting.results import summary, to_payload, write_result
from nomicronix.backtesting.strategy import Strategy
from nomicronix.backtesting.walk_forward import run_walk_forward_on_candles
from tests.backtesting.fakes import (
    HOUR_MS,
    JAN_1_2025_MS,
    BiasEntry,
    EntryOnce,
    ScriptedStrategy,
    make_candles,
    make_config,
)


def backtest_result() -> BacktestResult:
    config = make_config(
        start_ms=JAN_1_2025_MS,
        end_ms=JAN_1_2025_MS + 30 * HOUR_MS,
    )
    return run_backtest_on_candles(
        make_candles(30),
        EntryOnce(stop_pct=0.10, target_pct=10.0),
        config,
    )


def bias_factory(params: Mapping[str, object]) -> Strategy:
    return BiasEntry(**params)  # type: ignore[arg-type]


def walk_forward_result() -> WalkForwardResult:
    config = make_config(
        start_ms=JAN_1_2025_MS,
        end_ms=JAN_1_2025_MS + 30 * HOUR_MS,
    )
    wf = WalkForwardConfig(
        train_ms=5 * HOUR_MS,
        test_ms=5 * HOUR_MS,
        step_ms=5 * HOUR_MS,
        objective="total_return",
        min_train_trades=0,
        warmup_candles=0,
    )
    return run_walk_forward_on_candles(
        make_candles(30),
        bias_factory,
        {"bias": (1,)},
        config,
        wf,
    )


def test_backtest_payload_is_complete_and_typed() -> None:
    result = backtest_result()
    payload = to_payload(result)

    assert payload["schema_version"] == 1
    assert payload["kind"] == "backtest"
    assert payload["config"]["symbol"] == "BTC/USDT"
    assert payload["config"]["initial_cash"] == "1000"
    assert payload["config"]["fee_bps"] == "0"
    assert payload["config"]["position_fraction"] == "0.25"
    assert payload["config"]["quantity_step"] == "1"
    assert payload["data"]["candle_count"] == 30
    assert len(payload["trades"]) == len(result.trades)
    assert len(payload["equity_curve"]) == 30
    assert payload["metrics"]["trade_count"] == len(result.trades)
    assert payload["trades"][0]["exit_reason"] == "end_of_data"


def test_payload_is_json_serializable_without_nan_or_infinity() -> None:
    payload = to_payload(backtest_result())
    json.dumps(payload, allow_nan=False)


def test_written_json_is_byte_identical_across_runs(tmp_path: Path) -> None:
    result = backtest_result()
    first = write_result(result, tmp_path / "first.json")
    second = write_result(result, tmp_path / "second.json")

    assert first.read_bytes() == second.read_bytes()
    assert first.read_text(encoding="utf-8").endswith("\n")


def test_existing_output_requires_force(tmp_path: Path) -> None:
    result = backtest_result()
    destination = tmp_path / "result.json"
    write_result(result, destination)

    with pytest.raises(FileExistsError):
        write_result(result, destination)

    write_result(result, destination, force=True)
    assert destination.exists()


def test_write_result_creates_parent_directories(tmp_path: Path) -> None:
    destination = tmp_path / "a" / "b" / "result.json"
    write_result(backtest_result(), destination)
    assert destination.exists()


def test_walk_forward_payload_contains_windows_and_oos() -> None:
    result = walk_forward_result()
    payload = to_payload(result)

    assert payload["kind"] == "walk_forward"
    assert payload["walk_forward"]["train_ms"] == 5 * HOUR_MS
    assert payload["param_grid"] == {"bias": [1]}
    assert len(payload["windows"]) == len(result.windows) == 5
    assert len(payload["oos_equity_curve"]) == len(result.oos_equity_curve)
    assert payload["windows"][0]["params"] == {"bias": 1}
    assert set(payload["in_sample_mean"]) == {"total_return", "sharpe", "calmar"}


def test_summary_reports_metrics_and_missing_values() -> None:
    result = backtest_result()
    text = summary(result)
    assert "trades:" in text
    assert "total return:" in text

    flat_config = make_config(
        start_ms=JAN_1_2025_MS,
        end_ms=JAN_1_2025_MS + 30 * HOUR_MS,
    )
    flat = run_backtest_on_candles(make_candles(30), ScriptedStrategy({}), flat_config)
    flat_text = summary(flat)
    assert "sharpe: n/a" in flat_text
    assert "max drawdown: 0.00%" in flat_text


def test_decimal_strings_are_trimmed_of_trailing_zeros() -> None:
    result = backtest_result()
    payload = to_payload(result)
    assert payload["config"]["initial_cash"] == "1000"
    assert not payload["config"]["initial_cash"].endswith(".00")
    assert Decimal(payload["config"]["initial_cash"]) == Decimal("1000")
