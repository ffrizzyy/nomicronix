"""Backtesting domain types. Money is Decimal; ratios are floats."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class Action(StrEnum):
    ENTER_LONG = "enter_long"
    EXIT_LONG = "exit_long"


class ExitReason(StrEnum):
    STOP = "stop"
    TARGET = "target"
    SIGNAL = "signal"
    END_OF_DATA = "end_of_data"


class SkipReason(StrEnum):
    IGNORED_ENTRY = "ignored_entry"
    IGNORED_EXIT = "ignored_exit"
    SKIPPED_GAP = "skipped_gap"
    INSUFFICIENT_CASH = "insufficient_cash"
    INVALID_SIGNAL = "invalid_signal"
    END_OF_DATA_PENDING = "end_of_data_pending"


@dataclass(frozen=True, slots=True)
class Signal:
    action: Action
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    quantity: Decimal
    entry_time_ms: int
    entry_price: Decimal
    exit_time_ms: int
    exit_price: Decimal
    fees: Decimal
    pnl: Decimal
    return_pct: float
    exit_reason: ExitReason


@dataclass(frozen=True, slots=True)
class EquityPoint:
    time_ms: int
    equity: Decimal
    exposed: bool


@dataclass(frozen=True, slots=True)
class SimEvent:
    time_ms: int
    kind: str
    reason: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Metrics:
    total_return: float | None
    cagr: float | None
    max_drawdown: float | None
    sharpe: float | None
    sortino: float | None
    calmar: float | None
    win_rate: float | None
    profit_factor: float | None
    expectancy: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    fees_paid: Decimal
    turnover: Decimal
    exposure: float | None
    trade_count: int


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    symbol: str
    timeframe: str
    start_ms: int
    end_ms: int
    initial_cash: Decimal = Decimal("1000")
    fee_bps: Decimal = Decimal("10")
    slippage_bps: Decimal = Decimal("5")
    position_fraction: Decimal = Decimal("0.25")
    min_notional: Decimal = Decimal("10")
    quantity_step: Decimal = Decimal("0.00001")
    min_candles: int = 100
    warmup_candles: int = 0

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        if self.start_ms >= self.end_ms:
            raise ValueError("start_ms must be before end_ms")
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if self.fee_bps < 0 or self.slippage_bps < 0:
            raise ValueError("fees and slippage must not be negative")
        if not Decimal(0) < self.position_fraction <= Decimal(1):
            raise ValueError("position_fraction must be in (0, 1]")
        if self.min_notional <= 0:
            raise ValueError("min_notional must be positive")
        if self.quantity_step <= 0:
            raise ValueError("quantity_step must be positive")
        if self.min_candles < 1:
            raise ValueError("min_candles must be at least 1")
        if self.warmup_candles < 0:
            raise ValueError("warmup_candles must not be negative")


@dataclass(frozen=True, slots=True)
class BacktestResult:
    config: BacktestConfig
    first_candle_ms: int
    last_candle_ms: int
    candle_count: int
    trades: tuple[Trade, ...]
    equity_curve: tuple[EquityPoint, ...]
    events: tuple[SimEvent, ...]
    metrics: Metrics


@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    train_ms: int
    test_ms: int
    step_ms: int
    objective: str = "sharpe"
    min_train_trades: int = 10
    warmup_candles: int = 200
    allow_partial: bool = False

    def __post_init__(self) -> None:
        if min(self.train_ms, self.test_ms, self.step_ms) <= 0:
            raise ValueError("train, test and step must be positive")
        if self.objective not in {"sharpe", "total_return", "calmar"}:
            raise ValueError(f"unsupported objective: {self.objective}")
        if self.min_train_trades < 0:
            raise ValueError("min_train_trades must not be negative")
        if self.warmup_candles < 0:
            raise ValueError("warmup_candles must not be negative")


@dataclass(frozen=True, slots=True)
class WindowResult:
    train_start_ms: int
    train_end_ms: int
    test_start_ms: int
    test_end_ms: int
    params: dict[str, object]
    train: BacktestResult
    test: BacktestResult
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    config: BacktestConfig
    walk_forward: WalkForwardConfig
    param_grid: dict[str, tuple[object, ...]]
    windows: tuple[WindowResult, ...]
    oos_equity_curve: tuple[EquityPoint, ...]
    oos_trades: tuple[Trade, ...]
    metrics: Metrics
    in_sample_mean: dict[str, float | None]
    warnings: tuple[str, ...]
