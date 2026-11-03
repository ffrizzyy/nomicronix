"""Deterministic JSON serialization for backtest and walk-forward results."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

from nomicronix.backtesting.models import (
    BacktestConfig,
    BacktestResult,
    EquityPoint,
    Metrics,
    SimEvent,
    Trade,
    WalkForwardConfig,
    WalkForwardResult,
    WindowResult,
)


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _config_payload(config: BacktestConfig) -> dict[str, Any]:
    return {
        "symbol": config.symbol,
        "timeframe": config.timeframe,
        "start_ms": config.start_ms,
        "end_ms": config.end_ms,
        "initial_cash": _decimal_text(config.initial_cash),
        "fee_bps": _decimal_text(config.fee_bps),
        "slippage_bps": _decimal_text(config.slippage_bps),
        "position_fraction": _decimal_text(config.position_fraction),
        "min_notional": _decimal_text(config.min_notional),
        "quantity_step": _decimal_text(config.quantity_step),
        "min_candles": config.min_candles,
        "warmup_candles": config.warmup_candles,
    }


def _metrics_payload(metrics: Metrics) -> dict[str, Any]:
    return {
        "total_return": metrics.total_return,
        "cagr": metrics.cagr,
        "max_drawdown": metrics.max_drawdown,
        "sharpe": metrics.sharpe,
        "sortino": metrics.sortino,
        "calmar": metrics.calmar,
        "win_rate": metrics.win_rate,
        "profit_factor": metrics.profit_factor,
        "expectancy": None if metrics.expectancy is None else _decimal_text(metrics.expectancy),
        "average_win": None if metrics.average_win is None else _decimal_text(metrics.average_win),
        "average_loss": None
        if metrics.average_loss is None
        else _decimal_text(metrics.average_loss),
        "fees_paid": _decimal_text(metrics.fees_paid),
        "turnover": _decimal_text(metrics.turnover),
        "exposure": metrics.exposure,
        "trade_count": metrics.trade_count,
    }


def _trade_payload(trade: Trade) -> dict[str, Any]:
    return {
        "symbol": trade.symbol,
        "quantity": _decimal_text(trade.quantity),
        "entry_time_ms": trade.entry_time_ms,
        "entry_price": _decimal_text(trade.entry_price),
        "exit_time_ms": trade.exit_time_ms,
        "exit_price": _decimal_text(trade.exit_price),
        "fees": _decimal_text(trade.fees),
        "pnl": _decimal_text(trade.pnl),
        "return_pct": trade.return_pct,
        "exit_reason": trade.exit_reason.value,
    }


def _curve_payload(curve: tuple[EquityPoint, ...]) -> list[dict[str, Any]]:
    return [
        {
            "time_ms": point.time_ms,
            "equity": _decimal_text(point.equity),
            "exposed": point.exposed,
        }
        for point in curve
    ]


def _event_payload(event: SimEvent) -> dict[str, Any]:
    return {
        "time_ms": event.time_ms,
        "kind": event.kind,
        "reason": event.reason,
        "detail": event.detail,
    }


def _backtest_payload(result: BacktestResult) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "backtest",
        "config": _config_payload(result.config),
        "data": {
            "symbol": result.config.symbol,
            "timeframe": result.config.timeframe,
            "first_candle_ms": result.first_candle_ms,
            "last_candle_ms": result.last_candle_ms,
            "candle_count": result.candle_count,
        },
        "metrics": _metrics_payload(result.metrics),
        "trades": [_trade_payload(trade) for trade in result.trades],
        "equity_curve": _curve_payload(result.equity_curve),
        "events": [_event_payload(event) for event in result.events],
    }


def _window_payload(window: WindowResult) -> dict[str, Any]:
    return {
        "train_start_ms": window.train_start_ms,
        "train_end_ms": window.train_end_ms,
        "test_start_ms": window.test_start_ms,
        "test_end_ms": window.test_end_ms,
        "params": window.params,
        "train_metrics": _metrics_payload(window.train.metrics),
        "test_metrics": _metrics_payload(window.test.metrics),
        "train_trade_count": window.train.metrics.trade_count,
        "test_trade_count": window.test.metrics.trade_count,
        "warnings": list(window.warnings),
    }


def _walk_forward_payload(result: WalkForwardResult) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "walk_forward",
        "config": _config_payload(result.config),
        "walk_forward": _walk_forward_config_payload(result.walk_forward),
        "param_grid": {key: list(values) for key, values in result.param_grid.items()},
        "metrics": _metrics_payload(result.metrics),
        "in_sample_mean": result.in_sample_mean,
        "windows": [_window_payload(window) for window in result.windows],
        "oos_trades": [_trade_payload(trade) for trade in result.oos_trades],
        "oos_equity_curve": _curve_payload(result.oos_equity_curve),
        "warnings": list(result.warnings),
    }


def to_payload(result: BacktestResult | WalkForwardResult) -> dict[str, Any]:
    if isinstance(result, WalkForwardResult):
        return _walk_forward_payload(result)
    return _backtest_payload(result)


def write_result(
    result: BacktestResult | WalkForwardResult,
    path: str | Path,
    *,
    force: bool = False,
) -> Path:
    destination = Path(path)
    if destination.exists() and not force:
        raise FileExistsError(f"{destination} already exists; pass --force to replace it")
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(to_payload(result), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, destination)
    return destination


def summary(result: BacktestResult | WalkForwardResult) -> str:
    metrics = result.metrics
    return "\n".join(
        [
            f"trades: {metrics.trade_count}",
            f"total return: {_fmt_pct(metrics.total_return)}",
            f"max drawdown: {_fmt_pct(metrics.max_drawdown)}",
            f"sharpe: {_fmt(metrics.sharpe)}",
            f"sortino: {_fmt(metrics.sortino)}",
            f"profit factor: {_fmt(metrics.profit_factor)}",
            f"fees paid: {_decimal_text(metrics.fees_paid)}",
        ]
    )


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def _walk_forward_config_payload(wf: WalkForwardConfig) -> dict[str, Any]:
    return {
        "train_ms": wf.train_ms,
        "test_ms": wf.test_ms,
        "step_ms": wf.step_ms,
        "objective": wf.objective,
        "min_train_trades": wf.min_train_trades,
        "warmup_candles": wf.warmup_candles,
        "allow_partial": wf.allow_partial,
    }
