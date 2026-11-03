"""Signal propositions, scan configuration and result models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from nomicronix.market_data.models import Timeframe

DEFAULT_TTL_MS = 300_000
DEFAULT_MIN_HISTORY = 50
DEFAULT_MIN_CONFIDENCE = 0.5
DEFAULT_MAX_SIGNALS = 100


class Side(StrEnum):
    BUY = "buy"


class ProposalError(ValueError):
    """A trade idea violates the long-only signal contract."""


def decimal_text(value: Decimal) -> str:
    """Exact decimal text with trailing zeros trimmed; never scientific notation."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _positive(name: str, value: Decimal) -> None:
    if value <= 0:
        raise ProposalError(f"{name} must be positive, got {decimal_text(value)}")


def validate_confidence(value: float) -> None:
    if not 0.0 <= value <= 1.0:
        raise ProposalError(f"confidence must be inside [0, 1], got {value!r}")


def validate_geometry(entry: Decimal, stop: Decimal, target: Decimal) -> None:
    """Enforce a long-only idea: positive prices, stop below entry, target above."""
    _positive("entry_price", entry)
    _positive("stop_price", stop)
    _positive("target_price", target)
    if stop >= entry:
        raise ProposalError("stop_price must be below entry_price")
    if target <= entry:
        raise ProposalError("target_price must be above entry_price")


@dataclass(frozen=True, slots=True)
class SignalIdea:
    """A raw proposal from a strategy, before identity and timestamps are added."""

    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    confidence: float
    rationale: str
    features: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_geometry(self.entry_price, self.stop_price, self.target_price)
        validate_confidence(self.confidence)
        if not self.rationale.strip():
            raise ProposalError("rationale must not be empty")


@dataclass(frozen=True, slots=True)
class SignalCandidate:
    """A validated, identifiable signal ready for downstream risk checking."""

    signal_id: str
    strategy_id: str
    symbol: str
    timeframe: str
    side: Side
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    confidence: float
    rationale: str
    features: Mapping[str, str]
    created_at_ms: int
    expires_at_ms: int
    candle_open_time_ms: int

    def __post_init__(self) -> None:
        if not self.signal_id:
            raise ProposalError("signal_id must not be empty")
        if not self.strategy_id:
            raise ProposalError("strategy_id must not be empty")
        if not self.symbol:
            raise ProposalError("symbol must not be empty")
        try:
            Timeframe(self.timeframe)
        except ValueError as exc:
            raise ProposalError(str(exc)) from exc
        validate_geometry(self.entry_price, self.stop_price, self.target_price)
        validate_confidence(self.confidence)
        if not self.rationale.strip():
            raise ProposalError("rationale must not be empty")
        if self.created_at_ms <= self.candle_open_time_ms:
            raise ProposalError("created_at_ms must be after the candle open")
        if self.expires_at_ms <= self.created_at_ms:
            raise ProposalError("expires_at_ms must be after created_at_ms")


@dataclass(frozen=True, slots=True)
class ScanConfig:
    """Requested range, warm-up and signal policy for one scan."""

    symbol: str
    timeframe: str
    start_ms: int
    end_ms: int
    min_history: int = DEFAULT_MIN_HISTORY
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    ttl_ms: int = DEFAULT_TTL_MS
    max_signals: int = DEFAULT_MAX_SIGNALS

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        Timeframe(self.timeframe)
        if self.start_ms >= self.end_ms:
            raise ValueError("start_ms must be before end_ms")
        if self.min_history < 1:
            raise ValueError("min_history must be at least 1")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be inside [0, 1]")
        if self.ttl_ms <= 0:
            raise ValueError("ttl_ms must be positive")
        if self.max_signals < 1:
            raise ValueError("max_signals must be at least 1")


@dataclass(frozen=True, slots=True)
class SignalBatch:
    """The full, deterministic result of one scan."""

    config: ScanConfig
    strategy_id: str
    first_candle_ms: int
    last_candle_ms: int
    candle_count: int
    signals: tuple[SignalCandidate, ...]
    warnings: tuple[str, ...] = ()
