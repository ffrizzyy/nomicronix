"""Domain types for order execution.

Money is ``Decimal``; time is UTC epoch milliseconds. The domain never imports an
exchange library: only ``adapter.py`` may do that, lazily.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


def utc_now_ms() -> int:
    """Current UTC time in epoch milliseconds."""
    return time.time_ns() // 1_000_000


class ExecutionError(RuntimeError):
    """Base error with a stable code and the CLI exit code it maps to."""

    code = "execution_error"
    exit_code = 1

    def __init__(self, message: str, *, code: str | None = None, exit_code: int | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if exit_code is not None:
            self.exit_code = exit_code


class ConfigError(ExecutionError):
    """Execution configuration is missing, malformed or unsafe."""

    code = "config_error"
    exit_code = 1


class CredentialError(ExecutionError):
    """Exchange credentials are missing or malformed."""

    code = "credential_error"
    exit_code = 1


class StoreError(ExecutionError):
    """The execution journal is missing, corrupt or inconsistent."""

    code = "store_error"
    exit_code = 1


class IntentError(ExecutionError):
    """An intent is unknown or is not in a state that allows the command."""

    code = "intent_error"
    exit_code = 1


class RiskRejected(ExecutionError):
    """Risk refused the signal or the order submission."""

    code = "risk_rejected"
    exit_code = 2


class ExchangeError(ExecutionError):
    """The exchange failed or returned something that cannot be trusted."""

    code = "exchange_error"
    exit_code = 3


class ReconciliationError(ExchangeError):
    """Local and exchange books do not agree; trading is blocked."""

    code = "reconciliation_error"
    exit_code = 3


class LiveDisabled(ExecutionError):
    """Live submission is disabled by the stage, the config or the missing flag."""

    code = "live_disabled"
    exit_code = 4


class IntentState(StrEnum):
    SHADOW = "shadow"
    AWAITING_APPROVAL = "awaiting_approval"
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    FAILED = "failed"


class OrderStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def terminal(self) -> bool:
        return self is not OrderStatus.OPEN


class EventType(StrEnum):
    INTENT_CREATED = "intent_created"
    SHADOWED = "shadowed"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    FAILED = "failed"
    RECONCILED = "reconciled"


TERMINAL_STATES = frozenset(
    {IntentState.FILLED, IntentState.CANCELED, IntentState.REJECTED, IntentState.FAILED}
)


def intent_id_for(signal_id: str) -> str:
    """The client order id is derived from the signal id, never invented."""
    return f"nx-{signal_id}"


def _positive(name: str, value: Decimal) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """One approved signal expressed as a proposed spot order."""

    intent_id: str
    signal_id: str
    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    approved_notional: Decimal
    created_at_ms: int
    expires_at_ms: int
    state: IntentState
    filled_quantity: Decimal = Decimal(0)
    exchange_order_id: str | None = None

    def __post_init__(self) -> None:
        if not self.signal_id.strip():
            raise ValueError("signal_id must not be empty")
        if self.intent_id != intent_id_for(self.signal_id):
            raise ValueError("intent_id must be derived from signal_id")
        if not self.symbol.strip():
            raise ValueError("symbol must not be empty")
        if self.side != "buy":
            raise ValueError("execution supports buy intents only")
        if self.quantity < 0:
            raise ValueError("quantity must not be negative")
        if self.quantity == 0 and self.state is not IntentState.REJECTED:
            raise ValueError("a submittable intent needs a positive quantity")
        _positive("entry_price", self.entry_price)
        _positive("stop_price", self.stop_price)
        _positive("target_price", self.target_price)
        if not self.stop_price < self.entry_price < self.target_price:
            raise ValueError("intent requires stop < entry < target")
        if self.approved_notional < 0:
            raise ValueError("approved_notional must not be negative")
        if self.created_at_ms <= 0:
            raise ValueError("created_at_ms must be positive")
        if self.expires_at_ms <= self.created_at_ms:
            raise ValueError("expires_at_ms must be after created_at_ms")
        if not Decimal(0) <= self.filled_quantity <= self.quantity:
            raise ValueError("filled_quantity must be inside [0, quantity]")

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity


@dataclass(frozen=True, slots=True)
class Ticker:
    """Best bid/ask for one symbol, already converted to Decimal."""

    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    at_ms: int

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("ticker symbol must not be empty")
        _positive("bid", self.bid)
        _positive("ask", self.ask)
        _positive("last", self.last)
        if self.bid > self.ask:
            raise ValueError("ticker bid must not exceed ask")
        if self.at_ms <= 0:
            raise ValueError("ticker timestamp must be positive")


@dataclass(frozen=True, slots=True)
class ExchangeOrder:
    """The exchange's view of one order."""

    client_order_id: str
    exchange_order_id: str
    symbol: str
    side: str
    status: OrderStatus
    quantity: Decimal
    filled_quantity: Decimal
    average_price: Decimal | None
    fee: Decimal
    updated_at_ms: int
    raw_status: str

    def __post_init__(self) -> None:
        if not self.client_order_id.strip():
            raise ValueError("client_order_id must not be empty")
        if not self.exchange_order_id.strip():
            raise ValueError("exchange_order_id must not be empty")
        if not self.symbol.strip():
            raise ValueError("symbol must not be empty")
        _positive("quantity", self.quantity)
        if not Decimal(0) <= self.filled_quantity <= self.quantity:
            raise ValueError("filled_quantity must be inside [0, quantity]")
        if self.filled_quantity > 0:
            if self.average_price is None or self.average_price <= 0:
                raise ValueError("a filled order needs a positive average price")
        elif self.average_price is not None:
            raise ValueError("an unfilled order must not carry an average price")
        if self.fee < 0:
            raise ValueError("fee must not be negative")
        if self.updated_at_ms <= 0:
            raise ValueError("updated_at_ms must be positive")

    @property
    def terminal(self) -> bool:
        return self.status.terminal


@dataclass(frozen=True, slots=True)
class OrderEvent:
    """One append-only journal row."""

    intent_id: str
    event_type: EventType
    at_ms: int
    payload: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.intent_id.strip():
            raise ValueError("intent_id must not be empty")
        if self.at_ms <= 0:
            raise ValueError("at_ms must be positive")


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """The result of comparing the journal with the exchange."""

    matched: tuple[str, ...] = ()
    unknown_orders: tuple[str, ...] = ()
    missing_orders: tuple[str, ...] = ()
    unknown_positions: tuple[str, ...] = ()
    detail: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not (self.unknown_orders or self.missing_orders or self.unknown_positions)


@dataclass(frozen=True, slots=True)
class FillReport:
    """Aggregated exchange fills for one intent."""

    intent_id: str
    filled_quantity: Decimal
    average_price: Decimal | None
    fee: Decimal
    complete: bool

    def __post_init__(self) -> None:
        if self.filled_quantity < 0:
            raise ValueError("filled_quantity must not be negative")
        if self.fee < 0:
            raise ValueError("fee must not be negative")
        if self.filled_quantity > 0:
            if self.average_price is None or self.average_price <= 0:
                raise ValueError("a filled report needs a positive average price")
        elif self.average_price is not None:
            raise ValueError("an unfilled report must not carry an average price")
