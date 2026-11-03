"""The validated extension point for a future AI/LLM proposer.

v1 ships no provider and makes no network call. A provider adapter must return
`AIProposal` objects; the module validates them exactly like rule-based ideas.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from nomicronix.strategy_engine.models import (
    ProposalError,
    SignalIdea,
    validate_confidence,
    validate_geometry,
)
from nomicronix.strategy_engine.views import CandleView

CONSTRAINTS: tuple[str, ...] = (
    "long only, spot",
    "entry, stop and target are required",
    "stop must be below entry and target above entry",
    "confidence must be between 0 and 1",
    "a non-empty rationale is required",
)


@dataclass(frozen=True, slots=True)
class ProposalRequest:
    """A bounded, causal market summary handed to an AI proposer."""

    symbol: str
    timeframe: str
    candle_open_time_ms: int
    close_price: Decimal
    recent_closes: tuple[Decimal, ...]
    constraints: tuple[str, ...] = CONSTRAINTS


def request_from_view(view: CandleView, history_limit: int = 20) -> ProposalRequest:
    """Build a request from the visible candles only; never reads later data."""
    if history_limit < 1:
        raise ValueError("history_limit must be at least 1")
    closes = tuple(candle.close for candle in view[-history_limit:])
    return ProposalRequest(
        symbol=view.candle.symbol,
        timeframe=view.candle.timeframe,
        candle_open_time_ms=view.candle.open_time_ms,
        close_price=view.candle.close,
        recent_closes=closes,
    )


@dataclass(frozen=True, slots=True)
class AIProposal:
    """A model output that still has to pass validation and then risk."""

    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    confidence: float
    rationale: str
    model_id: str

    def __post_init__(self) -> None:
        validate_geometry(self.entry_price, self.stop_price, self.target_price)
        validate_confidence(self.confidence)
        if not self.rationale.strip():
            raise ProposalError("rationale must not be empty")
        if not self.model_id.strip():
            raise ProposalError("model_id must not be empty")

    def to_idea(self) -> SignalIdea:
        return SignalIdea(
            entry_price=self.entry_price,
            stop_price=self.stop_price,
            target_price=self.target_price,
            confidence=self.confidence,
            rationale=self.rationale,
            features={"model_id": self.model_id},
        )


class AIProposer(Protocol):
    model_id: str

    def propose(self, request: ProposalRequest) -> AIProposal | None:
        """Return a validated proposal or None. Implementations must not trade."""
        ...
