from decimal import Decimal

from nomicronix.risk_engine.models import RiskLimits
from nomicronix.risk_engine.sizing import floor_to_step, meets_minimum, size_position
from tests.risk_engine.fakes import make_instrument


def test_standard_conservative_fixture() -> None:
    result = size_position(
        Decimal("1000"),
        Decimal("100"),
        Decimal("95"),
        RiskLimits(),
        make_instrument(),
    )
    assert result.quantity == Decimal("1")
    assert result.notional == Decimal("100")
    assert result.risk_amount == Decimal("5")
    assert result.capped_by_notional is False


def test_notional_cap_reduces_size_without_rounding_up() -> None:
    result = size_position(
        Decimal("1000"),
        Decimal("10"),
        Decimal("9.9"),
        RiskLimits(),
        make_instrument(),
    )
    assert result.quantity == Decimal("25")
    assert result.notional == Decimal("250")
    assert result.risk_amount == Decimal("2.5")
    assert result.capped_by_notional is True
    assert result.risk_amount <= Decimal("5")


def test_step_rounding_always_rounds_down() -> None:
    result = size_position(
        Decimal("1000"),
        Decimal("100"),
        Decimal("95"),
        RiskLimits(),
        make_instrument(step="0.4"),
    )
    assert result.quantity == Decimal("0.8")
    assert result.risk_amount == Decimal("4.0")


def test_floor_to_step_rejects_non_positive_step() -> None:
    try:
        floor_to_step(Decimal("1"), Decimal("0"))
    except ValueError as exc:
        assert "step" in str(exc)
    else:  # pragma: no cover - explicit failure path
        raise AssertionError("expected ValueError")


def test_minimum_requires_quantity_and_notional() -> None:
    limits = RiskLimits()
    instrument = make_instrument()
    assert meets_minimum(Decimal("1"), Decimal("100"), limits, instrument)
    assert not meets_minimum(Decimal("0.000001"), Decimal("100"), limits, instrument)
    assert not meets_minimum(Decimal("1"), Decimal("9.99"), limits, instrument)


def test_zero_equity_returns_zero() -> None:
    result = size_position(
        Decimal("0"), Decimal("100"), Decimal("95"), RiskLimits(), make_instrument()
    )
    assert result.quantity == Decimal(0)
    assert result.notional == Decimal(0)
    assert result.risk_amount == Decimal(0)
