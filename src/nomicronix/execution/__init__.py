"""Order execution for Nomicronix: shadow and human-approved spot orders only.

The module never submits without an approved risk decision, the exact
confirmation phrase, ``dry_run = false`` and ``--live``. It has no withdrawal
path and no autonomous mode.
"""

from nomicronix.execution.models import (
    EventType,
    ExchangeError,
    ExchangeOrder,
    ExecutionError,
    FillReport,
    IntentError,
    IntentState,
    LiveDisabled,
    OrderEvent,
    OrderIntent,
    OrderStatus,
    Reconciliation,
    ReconciliationError,
    RiskRejected,
    StoreError,
    Ticker,
    intent_id_for,
)

__all__ = [
    "EventType",
    "ExchangeError",
    "ExchangeOrder",
    "ExecutionError",
    "FillReport",
    "IntentError",
    "IntentState",
    "LiveDisabled",
    "OrderEvent",
    "OrderIntent",
    "OrderStatus",
    "Reconciliation",
    "ReconciliationError",
    "RiskRejected",
    "StoreError",
    "Ticker",
    "intent_id_for",
]
