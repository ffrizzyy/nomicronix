from decimal import Decimal

import pytest

from nomicronix.strategy_engine.models import (
    ProposalError,
    ScanConfig,
    Side,
    SignalCandidate,
    SignalIdea,
    decimal_text,
)
from tests.strategy_engine.fakes import HOUR_MS, JAN_1_2025_MS


def idea(**overrides: object) -> SignalIdea:
    values: dict[str, object] = {
        "entry_price": Decimal("100"),
        "stop_price": Decimal("95"),
        "target_price": Decimal("115"),
        "confidence": 0.6,
        "rationale": "test",
        "features": {"atr": "2.5"},
    }
    values.update(overrides)
    return SignalIdea(**values)  # type: ignore[arg-type]


def candidate(**overrides: object) -> SignalCandidate:
    open_ms = JAN_1_2025_MS
    values: dict[str, object] = {
        "signal_id": "sma-cross:BTC/USDT:1h:1735689600000",
        "strategy_id": "sma-cross",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "side": Side.BUY,
        "entry_price": Decimal("100"),
        "stop_price": Decimal("95"),
        "target_price": Decimal("115"),
        "confidence": 0.6,
        "rationale": "test",
        "features": {},
        "created_at_ms": open_ms + HOUR_MS,
        "expires_at_ms": open_ms + HOUR_MS + 300_000,
        "candle_open_time_ms": open_ms,
    }
    values.update(overrides)
    return SignalCandidate(**values)  # type: ignore[arg-type]


def test_valid_idea_and_candidate() -> None:
    assert idea().entry_price == Decimal("100")
    result = candidate()
    assert result.side is Side.BUY
    assert result.expires_at_ms > result.created_at_ms


@pytest.mark.parametrize(
    "overrides",
    [
        {"entry_price": Decimal("0")},
        {"stop_price": Decimal("-1")},
        {"target_price": Decimal("0")},
        {"stop_price": Decimal("100")},
        {"stop_price": Decimal("101")},
        {"target_price": Decimal("100")},
        {"target_price": Decimal("99")},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"confidence": float("nan")},
        {"rationale": "   "},
        {"rationale": ""},
    ],
)
def test_invalid_idea_is_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ProposalError):
        idea(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"signal_id": ""},
        {"strategy_id": ""},
        {"symbol": ""},
        {"timeframe": "7m"},
        {"created_at_ms": JAN_1_2025_MS},
        {"created_at_ms": JAN_1_2025_MS - 1},
        {"expires_at_ms": JAN_1_2025_MS + HOUR_MS},
        {"expires_at_ms": JAN_1_2025_MS + HOUR_MS - 1},
        {"entry_price": Decimal("0")},
        {"confidence": 2.0},
        {"rationale": " "},
    ],
)
def test_invalid_candidate_is_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ProposalError):
        candidate(**overrides)


def test_scan_config_defaults_are_conservative() -> None:
    config = ScanConfig(
        symbol="BTC/USDT",
        timeframe="1h",
        start_ms=JAN_1_2025_MS,
        end_ms=JAN_1_2025_MS + HOUR_MS,
    )
    assert config.min_history == 50
    assert config.min_confidence == 0.5
    assert config.ttl_ms == 300_000
    assert config.max_signals == 100


@pytest.mark.parametrize(
    "overrides",
    [
        {"symbol": ""},
        {"timeframe": "2h"},
        {"start_ms": 2, "end_ms": 1},
        {"start_ms": 1, "end_ms": 1},
        {"min_history": 0},
        {"min_confidence": -0.1},
        {"min_confidence": 1.1},
        {"ttl_ms": 0},
        {"max_signals": 0},
    ],
)
def test_invalid_scan_config_is_rejected(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "start_ms": JAN_1_2025_MS,
        "end_ms": JAN_1_2025_MS + HOUR_MS,
    }
    values.update(overrides)
    with pytest.raises(ValueError):
        ScanConfig(**values)  # type: ignore[arg-type]


def test_decimal_text_trims_without_scientific_notation() -> None:
    assert decimal_text(Decimal("100.00")) == "100"
    assert decimal_text(Decimal("0.5000")) == "0.5"
    assert decimal_text(Decimal("0")) == "0"
    assert decimal_text(Decimal("1E+2")) == "100"
