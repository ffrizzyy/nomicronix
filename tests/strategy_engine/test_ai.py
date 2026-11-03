from decimal import Decimal

import pytest

from nomicronix.strategy_engine.ai import (
    AIProposal,
    ProposalRequest,
    request_from_view,
)
from nomicronix.strategy_engine.models import ProposalError
from nomicronix.strategy_engine.views import CandleView
from tests.strategy_engine.fakes import make_candles


def proposal(**overrides: object) -> AIProposal:
    values: dict[str, object] = {
        "entry_price": Decimal("100"),
        "stop_price": Decimal("95"),
        "target_price": Decimal("115"),
        "confidence": 0.7,
        "rationale": "model saw a breakout",
        "model_id": "test-model-v1",
    }
    values.update(overrides)
    return AIProposal(**values)  # type: ignore[arg-type]


def test_ai_proposal_converts_to_a_validated_idea() -> None:
    idea = proposal().to_idea()
    assert idea.entry_price == Decimal("100")
    assert idea.confidence == 0.7
    assert idea.features == {"model_id": "test-model-v1"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"entry_price": Decimal("0")},
        {"stop_price": Decimal("100")},
        {"target_price": Decimal("100")},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"rationale": " "},
        {"model_id": ""},
    ],
)
def test_invalid_ai_proposal_is_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ProposalError):
        proposal(**overrides)


def test_request_uses_only_visible_candles() -> None:
    candles = make_candles(10)
    view = CandleView(candles, 5)

    request = request_from_view(view, history_limit=3)

    assert request.symbol == "BTC/USDT"
    assert request.timeframe == "1h"
    assert request.candle_open_time_ms == candles[4].open_time_ms
    assert request.close_price == candles[4].close
    assert request.recent_closes == tuple(candle.close for candle in candles[2:5])
    assert "long only, spot" in request.constraints


def test_request_rejects_a_bad_history_limit() -> None:
    view = CandleView(make_candles(3), 2)
    with pytest.raises(ValueError):
        request_from_view(view, history_limit=0)


def test_a_fake_ai_proposer_can_implement_the_protocol() -> None:
    class FakeAI:
        model_id = "fake"

        def propose(self, request: ProposalRequest) -> AIProposal | None:
            if request.close_price <= 0:
                return None
            return proposal(model_id=self.model_id)

    candles = make_candles(3)
    result = FakeAI().propose(request_from_view(CandleView(candles, 2)))
    assert result is not None
    assert result.to_idea().features["model_id"] == "fake"
