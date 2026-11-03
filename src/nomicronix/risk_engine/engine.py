"""Pure, deterministic rule evaluation. No I/O, clock, network or globals."""

from __future__ import annotations

from decimal import Decimal

from nomicronix.risk_engine.models import (
    Instrument,
    RejectReason,
    RiskDecision,
    RiskLimits,
    RiskSnapshot,
    Side,
    TradeProposal,
)
from nomicronix.risk_engine.sizing import meets_minimum, size_position


def _add(reasons: list[RejectReason], reason: RejectReason) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _valid_proposal(proposal: TradeProposal) -> bool:
    if not proposal.signal_id.strip():
        return False
    if proposal.side is not Side.BUY:
        return False
    if proposal.entry_price <= 0 or proposal.stop_price <= 0:
        return False
    if proposal.target_price <= 0:
        return False
    if not proposal.stop_price < proposal.entry_price < proposal.target_price:
        return False
    if not 0.0 <= proposal.confidence <= 1.0:
        return False
    return proposal.created_at_ms > 0


def evaluate(
    proposal: TradeProposal,
    snapshot: RiskSnapshot,
    instrument: Instrument,
    limits: RiskLimits,
    now_ms: int,
) -> RiskDecision:
    """Evaluate one proposal against one snapshot and return a full decision.

    Every applicable rule is checked so the journal explains all failures. Only
    invalid prices stop the arithmetic early, because later checks are meaningless.
    """
    reasons: list[RejectReason] = []

    if snapshot.kill_switch is not None:
        _add(reasons, RejectReason.KILL_SWITCH)

    signal_age_ms = now_ms - proposal.created_at_ms
    if signal_age_ms < 0 or signal_age_ms > limits.max_signal_age_ms:
        _add(reasons, RejectReason.STALE_SIGNAL)

    state_age_ms = now_ms - snapshot.as_of_ms
    if state_age_ms < 0 or state_age_ms > limits.max_signal_age_ms:
        _add(reasons, RejectReason.STALE_STATE)

    if proposal.symbol not in limits.allowed_symbols:
        _add(reasons, RejectReason.SYMBOL_NOT_ALLOWED)
    if instrument.symbol != proposal.symbol:
        _add(reasons, RejectReason.INSTRUMENT_MISMATCH)

    valid = _valid_proposal(proposal)
    if not valid:
        _add(reasons, RejectReason.INVALID_PROPOSAL)
        return RiskDecision(
            approved=False,
            signal_id=proposal.signal_id,
            approved_quantity=Decimal(0),
            approved_notional=Decimal(0),
            risk_amount=Decimal(0),
            reward_risk=Decimal(0),
            reasons=tuple(reasons),
            rule_trace=(
                f"equity={snapshot.equity}",
                f"position_count={snapshot.position_count}",
                "invalid_proposal",
            ),
        )

    if any(position.symbol == proposal.symbol for position in snapshot.positions):
        _add(reasons, RejectReason.DUPLICATE_POSITION)
    if snapshot.position_count >= limits.max_concurrent_positions:
        _add(reasons, RejectReason.MAX_POSITIONS)

    if snapshot.daily_loss_pct >= limits.max_daily_loss_pct:
        _add(reasons, RejectReason.DAILY_LOSS_LIMIT)
    if snapshot.drawdown_pct >= limits.max_drawdown_pct:
        _add(reasons, RejectReason.MAX_DRAWDOWN)

    reward_risk = (proposal.target_price - proposal.entry_price) / (
        proposal.entry_price - proposal.stop_price
    )
    if reward_risk < limits.min_reward_risk:
        _add(reasons, RejectReason.REWARD_RISK_TOO_LOW)

    sizing = size_position(
        equity=snapshot.equity,
        entry=proposal.entry_price,
        stop=proposal.stop_price,
        limits=limits,
        instrument=instrument,
    )
    if not meets_minimum(sizing.quantity, sizing.notional, limits, instrument):
        _add(reasons, RejectReason.SIZE_BELOW_MINIMUM)

    if snapshot.exposure + sizing.notional > snapshot.equity * limits.max_total_exposure_pct:
        _add(reasons, RejectReason.MAX_TOTAL_EXPOSURE)
    if snapshot.open_risk + sizing.risk_amount > snapshot.equity * limits.max_open_risk_pct:
        _add(reasons, RejectReason.MAX_OPEN_RISK)

    approved = not reasons
    return RiskDecision(
        approved=approved,
        signal_id=proposal.signal_id,
        approved_quantity=sizing.quantity,
        approved_notional=sizing.notional,
        risk_amount=sizing.risk_amount,
        reward_risk=reward_risk,
        reasons=tuple(reasons),
        rule_trace=(
            f"equity={snapshot.equity}",
            f"position_count={snapshot.position_count}",
            f"daily_loss_pct={snapshot.daily_loss_pct}",
            f"drawdown_pct={snapshot.drawdown_pct}",
            f"reward_risk={reward_risk}",
            f"quantity={sizing.quantity}",
            f"notional={sizing.notional}",
            f"risk_amount={sizing.risk_amount}",
            f"capped_by_notional={sizing.capped_by_notional}",
            "approved" if approved else "rejected",
        ),
    )
