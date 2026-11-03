"""Backtesting: causal strategy simulation with costs and walk-forward validation."""

from nomicronix.backtesting.data import DataError, DataGapError, InsufficientDataError
from nomicronix.backtesting.engine import run_backtest, run_backtest_on_candles
from nomicronix.backtesting.models import (
    Action,
    BacktestConfig,
    BacktestResult,
    EquityPoint,
    ExitReason,
    Metrics,
    Signal,
    SimEvent,
    Trade,
    WalkForwardConfig,
    WalkForwardResult,
    WindowResult,
)
from nomicronix.backtesting.strategy import HistoryView, Strategy, StrategyFactory
from nomicronix.backtesting.walk_forward import WalkForwardError, run_walk_forward

__all__ = [
    "Action",
    "BacktestConfig",
    "BacktestResult",
    "DataError",
    "DataGapError",
    "EquityPoint",
    "ExitReason",
    "HistoryView",
    "InsufficientDataError",
    "Metrics",
    "Signal",
    "SimEvent",
    "Strategy",
    "StrategyFactory",
    "Trade",
    "WalkForwardConfig",
    "WalkForwardError",
    "WalkForwardResult",
    "WindowResult",
    "run_backtest",
    "run_backtest_on_candles",
    "run_walk_forward",
]
