"""Position sizing and aggregate-risk arithmetic. Pure Decimal math."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from nomicronix.risk_engine.models import Instrument, RiskLimits


@dataclass(frozen=True, slots=True)
class SizingResult:
    quantity: Decimal
    notional: Decimal
    risk_amount: Decimal
    capped_by_notional: bool


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round down to a multiple of `step`. Never rounds up."""
    if step <= 0:
        raise ValueError("step must be positive")
    units = (value / step).to_integral_value(rounding=ROUND_FLOOR)
    return units * step


def size_position(
    equity: Decimal,
    entry: Decimal,
    stop: Decimal,
    limits: RiskLimits,
    instrument: Instrument,
) -> SizingResult:
    """Largest quantity that keeps loss at the stop within the per-trade limit."""
    risk_per_unit = entry - stop
    if equity <= 0 or entry <= 0 or risk_per_unit <= 0:
        return SizingResult(Decimal(0), Decimal(0), Decimal(0), False)

    by_risk = (equity * limits.risk_per_trade_pct) / risk_per_unit
    by_notional = (equity * limits.max_position_notional_pct) / entry
    capped = by_notional < by_risk
    quantity = floor_to_step(min(by_risk, by_notional), instrument.quantity_step)
    notional = quantity * entry
    risk_amount = quantity * risk_per_unit
    return SizingResult(quantity, notional, risk_amount, capped)


def meets_minimum(
    quantity: Decimal,
    notional: Decimal,
    limits: RiskLimits,
    instrument: Instrument,
) -> bool:
    """True when a sized order is tradable on the exchange and per this spec."""
    if quantity <= 0 or quantity < instrument.min_quantity:
        return False
    minimum_notional = max(instrument.min_notional, limits.min_notional_usdt)
    return notional >= minimum_notional
