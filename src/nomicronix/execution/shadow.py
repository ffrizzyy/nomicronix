"""Shadow-mode guards and intent construction."""

from __future__ import annotations

from decimal import Decimal

from nomicronix.execution.config import ExecutionConfig
from nomicronix.execution.models import (
    IntentState,
    OrderIntent,
    Ticker,
    intent_id_for,
)
from nomicronix.risk_engine.models import RiskDecision
from nomicronix.strategy_engine.models import SignalCandidate


def slippage_bps(entry: Decimal, ask: Decimal) -> Decimal:
    """Adverse move from the signal entry to the current best ask, in basis points."""
    return (ask - entry) / entry * Decimal(10_000)


def approved_state(config: ExecutionConfig) -> IntentState:
    if config.stage == "shadow":
        return IntentState.SHADOW
    return IntentState.AWAITING_APPROVAL


def guard_reasons(
    signal: SignalCandidate,
    decision: RiskDecision,
    ticker: Ticker | None,
    config: ExecutionConfig,
    now_ms: int,
) -> tuple[str, ...]:
    """Every reason this approved signal must still not become an order."""
    if not decision.approved:
        return tuple(str(reason.value) for reason in decision.reasons)
    reasons: list[str] = []
    if now_ms >= signal.expires_at_ms:
        reasons.append("expired")
    if now_ms - signal.created_at_ms > config.max_intent_age_ms:
        reasons.append("stale_intent")
    if ticker is not None and slippage_bps(signal.entry_price, ticker.ask) > Decimal(
        config.max_slippage_bps
    ):
        reasons.append("slippage")
    return tuple(reasons)


def build_intent(
    signal: SignalCandidate,
    decision: RiskDecision,
    *,
    state: IntentState,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id_for(signal.signal_id),
        signal_id=signal.signal_id,
        symbol=signal.symbol,
        side="buy",
        quantity=decision.approved_quantity,
        entry_price=signal.entry_price,
        stop_price=signal.stop_price,
        target_price=signal.target_price,
        approved_notional=decision.approved_notional,
        created_at_ms=signal.created_at_ms,
        expires_at_ms=signal.expires_at_ms,
        state=state,
    )


def build_rejected_intent(signal: SignalCandidate) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id_for(signal.signal_id),
        signal_id=signal.signal_id,
        symbol=signal.symbol,
        side="buy",
        quantity=Decimal(0),
        entry_price=signal.entry_price,
        stop_price=signal.stop_price,
        target_price=signal.target_price,
        approved_notional=Decimal(0),
        created_at_ms=signal.created_at_ms,
        expires_at_ms=signal.expires_at_ms,
        state=IntentState.REJECTED,
    )
