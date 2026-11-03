"""Delivery notifications, paper trades and outbox records."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from nomicronix.market_data.models import Timeframe
from nomicronix.risk_engine.models import Instrument
from nomicronix.strategy_engine.models import Side


class DeliveryError(RuntimeError):
    """A configured sink could not deliver a notification."""


class ConfigError(ValueError):
    """Delivery configuration is missing, malformed or unsafe."""


class StoreError(RuntimeError):
    """The delivery database is missing, corrupt or inconsistent."""


class SignalFileError(ValueError):
    """A strategy-engine result file cannot be trusted."""


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    REJECTED = "rejected"
    FAILED = "failed"


class DeliveryAction(StrEnum):
    DELIVERED = "delivered"
    SKIPPED = "skipped"
    REJECTED = "rejected"
    RETRIED = "retried"


def _positive(name: str, value: Decimal) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")


@dataclass(frozen=True, slots=True)
class InstrumentRules:
    """Exchange quantity rules applied to every symbol in a delivery run."""

    quantity_step: Decimal = Decimal("0.00001")
    min_quantity: Decimal = Decimal("0.00001")
    min_notional: Decimal = Decimal("10")

    def __post_init__(self) -> None:
        _positive("quantity_step", self.quantity_step)
        if self.min_quantity < 0:
            raise ValueError("min_quantity must not be negative")
        if self.min_notional < 0:
            raise ValueError("min_notional must not be negative")

    def for_symbol(self, symbol: str) -> Instrument:
        return Instrument(
            symbol=symbol,
            quantity_step=self.quantity_step,
            min_quantity=self.min_quantity,
            min_notional=self.min_notional,
        )


@dataclass(frozen=True, slots=True)
class Notification:
    """The stable payload a sink receives for one signal candidate."""

    signal_id: str
    strategy_id: str
    symbol: str
    timeframe: str
    side: str
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    confidence: float
    rationale: str
    created_at_ms: int
    expires_at_ms: int
    approved: bool
    approved_quantity: Decimal
    approved_notional: Decimal
    risk_amount: Decimal
    reward_risk: Decimal
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.signal_id.strip():
            raise ValueError("signal_id must not be empty")
        if not self.strategy_id.strip():
            raise ValueError("strategy_id must not be empty")
        if not self.symbol.strip():
            raise ValueError("symbol must not be empty")
        Timeframe(self.timeframe)
        Side(self.side)
        _positive("entry_price", self.entry_price)
        _positive("stop_price", self.stop_price)
        _positive("target_price", self.target_price)
        if not self.stop_price < self.entry_price < self.target_price:
            raise ValueError("notification requires stop < entry < target")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be inside [0, 1]")
        if not self.rationale.strip():
            raise ValueError("rationale must not be empty")
        if self.created_at_ms <= 0:
            raise ValueError("created_at_ms must be positive")
        if self.expires_at_ms <= self.created_at_ms:
            raise ValueError("expires_at_ms must be after created_at_ms")
        if self.approved_quantity < 0 or self.approved_notional < 0:
            raise ValueError("approved size must not be negative")
        if self.risk_amount < 0 or self.reward_risk < 0:
            raise ValueError("risk figures must not be negative")
        if self.approved and self.reasons:
            raise ValueError("an approved notification cannot carry rejection reasons")
        if not self.approved and not self.reasons:
            raise ValueError("a rejected notification needs at least one reason")

    @property
    def status_text(self) -> str:
        return "approved" if self.approved else "rejected"


@dataclass(frozen=True, slots=True)
class PaperTrade:
    """One approved signal tracked as a full, fee-free paper position."""

    signal_id: str
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    opened_at_ms: int
    closed_at_ms: int | None = None
    exit_price: Decimal | None = None
    exit_reason: str | None = None
    realized_pnl: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.signal_id.strip() or not self.symbol.strip():
            raise ValueError("paper trade needs a signal id and symbol")
        _positive("quantity", self.quantity)
        _positive("entry_price", self.entry_price)
        _positive("stop_price", self.stop_price)
        _positive("target_price", self.target_price)
        if not self.stop_price < self.entry_price < self.target_price:
            raise ValueError("paper trade requires stop < entry < target")
        if self.opened_at_ms <= 0:
            raise ValueError("opened_at_ms must be positive")
        closed_fields = (self.closed_at_ms, self.exit_price, self.exit_reason, self.realized_pnl)
        if any(value is None for value in closed_fields) and not all(
            value is None for value in closed_fields
        ):
            raise ValueError("a paper trade is either fully open or fully closed")
        if self.exit_reason is not None and self.exit_reason not in {"stop", "target"}:
            raise ValueError("exit_reason must be stop or target")

    @property
    def is_open(self) -> bool:
        return self.closed_at_ms is None

    def pnl_at(self, price: Decimal) -> Decimal:
        return (price - self.entry_price) * self.quantity


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    """One outbox row, keyed by signal id."""

    signal_id: str
    status: DeliveryStatus
    payload_json: str
    recorded_at_ms: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    record: DeliveryRecord
    action: DeliveryAction


@dataclass(frozen=True, slots=True)
class DeliveryTotals:
    total: int = 0
    delivered: int = 0
    rejected: int = 0
    pending: int = 0
    failed: int = 0


@dataclass(frozen=True, slots=True)
class TickResult:
    closed: tuple[PaperTrade, ...]
    still_open: tuple[PaperTrade, ...]
