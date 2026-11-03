from decimal import Decimal

import pytest

from nomicronix.risk_engine.models import Instrument, KillSwitch, RiskLimits
from tests.risk_engine.fakes import (
    HOUR_MS,
    NOW_MS,
    make_position,
    make_snapshot,
)


def test_defaults_are_conservative() -> None:
    limits = RiskLimits()
    assert limits.risk_per_trade_pct == Decimal("0.005")
    assert limits.max_daily_loss_pct == Decimal("0.03")
    assert limits.max_drawdown_pct == Decimal("0.10")
    assert limits.max_concurrent_positions == 2
    assert limits.allowed_symbols == frozenset({"BTC/USDT", "ETH/USDT"})


@pytest.mark.parametrize("name", ["risk_per_trade_pct", "max_daily_loss_pct", "max_drawdown_pct"])
def test_percentages_must_be_inside_zero_and_one(name: str) -> None:
    with pytest.raises(ValueError):
        RiskLimits(**{name: Decimal("0")})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RiskLimits(**{name: Decimal("1.01")})  # type: ignore[arg-type]


def test_positions_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        RiskLimits(max_concurrent_positions=0)


def test_reward_risk_must_be_at_least_one() -> None:
    with pytest.raises(ValueError):
        RiskLimits(min_reward_risk=Decimal("0.5"))


def test_allowed_symbols_must_not_be_empty() -> None:
    with pytest.raises(ValueError):
        RiskLimits(allowed_symbols=frozenset())


def test_instrument_rejects_invalid_rules() -> None:
    with pytest.raises(ValueError):
        Instrument("", Decimal("1"), Decimal("0"), Decimal("1"))
    with pytest.raises(ValueError):
        Instrument("BTC/USDT", Decimal("0"), Decimal("0"), Decimal("1"))
    with pytest.raises(ValueError):
        Instrument("BTC/USDT", Decimal("1"), Decimal("-1"), Decimal("1"))


def test_snapshot_drawdown_and_daily_loss() -> None:
    snapshot = make_snapshot(equity="900", peak="1000", day_start="950")
    assert snapshot.drawdown_pct == Decimal("0.1")
    assert snapshot.daily_loss_pct == Decimal("0.05263157894736842105263157895")


def test_snapshot_exposure_and_open_risk() -> None:
    snapshot = make_snapshot(
        equity="1000",
        positions=(
            make_position(quantity="2", entry="100", stop="95", current="110"),
            make_position(symbol="ETH/USDT", quantity="1", entry="200", stop="190", current="210"),
        ),
    )
    assert snapshot.exposure == Decimal("430")
    assert snapshot.open_risk == Decimal("20")
    assert snapshot.position_count == 2


def test_snapshot_handles_zero_peak_without_dividing_by_zero() -> None:
    snapshot = make_snapshot(equity="0", peak="0", day_start="0")
    assert snapshot.drawdown_pct == Decimal(0)
    assert snapshot.daily_loss_pct == Decimal(0)


def test_kill_switch_is_immutable_data() -> None:
    kill = KillSwitch(reason="manual", engaged_at_ms=NOW_MS, engaged_by="owner")
    assert kill.reason == "manual"
    assert NOW_MS + HOUR_MS > kill.engaged_at_ms
