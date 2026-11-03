"""Domain types for the risk engine. Money is Decimal; time is UTC milliseconds."""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


def utc_now_ms() -> int:
    """Current UTC time in epoch milliseconds."""
    return time.time_ns() // 1_000_000


class Side(StrEnum):
    """Spot long-only for now. Shorts need a new spec and a new side."""

    BUY = "buy"


class FillAction(StrEnum):
    OPEN = "open"
    CLOSE = "close"


class RejectReason(StrEnum):
    KILL_SWITCH = "kill_switch"
    STALE_SIGNAL = "stale_signal"
    STALE_STATE = "stale_state"
    SYMBOL_NOT_ALLOWED = "symbol_not_allowed"
    INSTRUMENT_MISMATCH = "instrument_mismatch"
    INVALID_PROPOSAL = "invalid_proposal"
    DUPLICATE_POSITION = "duplicate_position"
    MAX_POSITIONS = "max_positions"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    MAX_DRAWDOWN = "max_drawdown"
    REWARD_RISK_TOO_LOW = "reward_risk_too_low"
    SIZE_BELOW_MINIMUM = "size_below_minimum"
    MAX_TOTAL_EXPOSURE = "max_total_exposure"
    MAX_OPEN_RISK = "max_open_risk"


@dataclass(frozen=True, slots=True)
class Instrument:
    """Exchange quantity rules for one symbol, supplied by the caller."""

    symbol: str
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("instrument symbol must not be empty")
        if self.quantity_step <= 0:
            raise ValueError("quantity_step must be positive")
        if self.min_quantity < 0:
            raise ValueError("min_quantity must not be negative")
        if self.min_notional < 0:
            raise ValueError("min_notional must not be negative")


@dataclass(frozen=True, slots=True)
class TradeProposal:
    signal_id: str
    strategy_id: str
    symbol: str
    side: Side
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    confidence: float
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    current_price: Decimal
    opened_at_ms: int
    signal_id: str
    entry_fee: Decimal = Decimal(0)

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.current_price

    @property
    def open_risk(self) -> Decimal:
        return self.quantity * max(self.entry_price - self.stop_price, Decimal(0))


@dataclass(frozen=True, slots=True)
class KillSwitch:
    reason: str
    engaged_at_ms: int
    engaged_by: str


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    equity: Decimal
    peak_equity: Decimal
    day_start_equity: Decimal
    realized_pnl_today: Decimal
    positions: tuple[Position, ...]
    kill_switch: KillSwitch | None
    as_of_ms: int

    @property
    def position_count(self) -> int:
        return len(self.positions)

    @property
    def exposure(self) -> Decimal:
        return sum((p.notional for p in self.positions), Decimal(0))

    @property
    def open_risk(self) -> Decimal:
        return sum((p.open_risk for p in self.positions), Decimal(0))

    @property
    def drawdown_pct(self) -> Decimal:
        if self.peak_equity <= 0:
            return Decimal(0)
        return max(
            (self.peak_equity - self.equity) / self.peak_equity,
            Decimal(0),
        )

    @property
    def daily_loss_pct(self) -> Decimal:
        if self.day_start_equity <= 0:
            return Decimal(0)
        return max(
            (self.day_start_equity - self.equity) / self.day_start_equity,
            Decimal(0),
        )


@dataclass(frozen=True, slots=True)
class RiskLimits:
    risk_per_trade_pct: Decimal = Decimal("0.005")
    max_daily_loss_pct: Decimal = Decimal("0.03")
    max_drawdown_pct: Decimal = Decimal("0.10")
    max_concurrent_positions: int = 2
    max_position_notional_pct: Decimal = Decimal("0.25")
    max_total_exposure_pct: Decimal = Decimal("0.50")
    max_open_risk_pct: Decimal = Decimal("0.02")
    min_reward_risk: Decimal = Decimal("2.0")
    min_notional_usdt: Decimal = Decimal("10")
    max_signal_age_ms: int = 300_000
    allowed_symbols: frozenset[str] = frozenset({"BTC/USDT", "ETH/USDT"})

    def __post_init__(self) -> None:
        for name in (
            "risk_per_trade_pct",
            "max_daily_loss_pct",
            "max_drawdown_pct",
            "max_position_notional_pct",
            "max_total_exposure_pct",
            "max_open_risk_pct",
        ):
            value = getattr(self, name)
            if not Decimal(0) < value <= Decimal(1):
                raise ValueError(f"{name} must be in (0, 1], got {value}")
        if self.max_concurrent_positions < 1:
            raise ValueError("max_concurrent_positions must be at least 1")
        if self.min_reward_risk < 1:
            raise ValueError("min_reward_risk must be at least 1")
        if self.min_notional_usdt <= 0:
            raise ValueError("min_notional_usdt must be positive")
        if self.max_signal_age_ms <= 0:
            raise ValueError("max_signal_age_ms must be positive")
        if not self.allowed_symbols:
            raise ValueError("allowed_symbols must not be empty")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    signal_id: str
    approved_quantity: Decimal
    approved_notional: Decimal
    risk_amount: Decimal
    reward_risk: Decimal
    reasons: tuple[RejectReason, ...]
    rule_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RiskStatus:
    equity: Decimal
    peak_equity: Decimal
    drawdown_pct: Decimal
    day_start_equity: Decimal
    daily_pnl: Decimal
    daily_loss_pct: Decimal
    open_positions: int
    open_risk: Decimal
    exposure: Decimal
    trading_allowed: bool
    kill_switch: KillSwitch | None
    last_mark_ms: int


@dataclass(frozen=True, slots=True)
class Fill:
    action: FillAction
    symbol: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    filled_at_ms: int


@dataclass(frozen=True, slots=True)
class Approval:
    signal_id: str
    symbol: str
    quantity: Decimal
    notional: Decimal
    stop_price: Decimal
    target_price: Decimal
    created_at_ms: int
    consumed_at_ms: int | None
    filled_quantity: Decimal = Decimal(0)
