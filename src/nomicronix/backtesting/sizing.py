"""Position sizing policies for the simulator."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol


class SizingPolicy(Protocol):
    def notional(self, equity: Decimal, cash: Decimal) -> Decimal:
        """Return the desired notional for a new position, capped by the caller."""
        ...


class FixedFractionSizer:
    """Spend a fixed fraction of current equity per trade."""

    def __init__(self, fraction: Decimal = Decimal("0.25")) -> None:
        if not Decimal(0) < fraction <= Decimal(1):
            raise ValueError("fraction must be in (0, 1]")
        self.fraction = fraction

    def notional(self, equity: Decimal, cash: Decimal) -> Decimal:
        return min(equity * self.fraction, cash)
