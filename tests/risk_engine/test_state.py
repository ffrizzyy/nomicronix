from decimal import Decimal

import pytest

from nomicronix.risk_engine.models import Fill, FillAction, KillSwitch, RiskLimits
from nomicronix.risk_engine.state import RESUME_PHRASE, PortfolioState, utc_day
from tests.risk_engine.fakes import NOW_MS


def open_fill(price: str = "100", quantity: str = "1", fee: str = "0") -> Fill:
    return Fill(
        FillAction.OPEN, "BTC/USDT", Decimal(quantity), Decimal(price), Decimal(fee), NOW_MS
    )


def close_fill(price: str = "110", quantity: str = "1", fee: str = "0") -> Fill:
    return Fill(
        FillAction.CLOSE, "BTC/USDT", Decimal(quantity), Decimal(price), Decimal(fee), NOW_MS
    )


def test_new_state_starts_flat_at_cash() -> None:
    state = PortfolioState.new(Decimal("1000"), NOW_MS)
    assert state.equity == Decimal("1000")
    assert state.peak_equity == Decimal("1000")
    assert state.day_start_equity == Decimal("1000")
    assert state.positions == {}
    assert state.kill_switch is None


def test_mark_updates_equity_peak_and_timestamp() -> None:
    state = PortfolioState.new(Decimal("1000"), NOW_MS)
    state.apply_open("sig-1", open_fill(), stop_price=Decimal("95"), target_price=Decimal("115"))
    snapshot = state.mark({"BTC/USDT": Decimal("110")}, NOW_MS + 1, RiskLimits())
    assert snapshot.equity == Decimal("1010")
    assert state.peak_equity == Decimal("1010")
    assert state.last_mark_ms == NOW_MS + 1


def test_drawdown_engages_kill_switch() -> None:
    state = PortfolioState.new(Decimal("1000"), NOW_MS)
    state.cash = Decimal("900")
    snapshot = state.mark({}, NOW_MS + 1, RiskLimits())
    assert snapshot.kill_switch is not None
    assert snapshot.kill_switch.reason == "max drawdown reached"
    assert snapshot.kill_switch.engaged_by == "risk-engine"


def test_day_rollover_resets_loss_budget_but_not_kill_switch() -> None:
    state = PortfolioState.new(Decimal("1000"), NOW_MS)
    state.cash = Decimal("950")
    state.day_start_equity = Decimal("1000")
    state.engage_kill("manual", NOW_MS, "owner")
    next_day = NOW_MS + 24 * 3_600_000

    state.mark({}, next_day, RiskLimits())

    assert state.day_utc == utc_day(next_day)
    assert state.day_start_equity == Decimal("950")
    assert state.realized_pnl_today == Decimal(0)
    assert state.kill_switch is not None


def test_day_rollover_does_not_retrigger_a_new_day_start_on_the_same_day() -> None:
    state = PortfolioState.new(Decimal("1000"), NOW_MS)
    state.cash = Decimal("900")
    state.mark({}, NOW_MS + 1, RiskLimits())
    day_after_mark = state.day_start_equity
    state.mark({}, NOW_MS + 2, RiskLimits())
    assert state.day_start_equity == day_after_mark


def test_open_and_close_fill_accounting_includes_fees() -> None:
    state = PortfolioState.new(Decimal("1000"), NOW_MS)
    state.apply_open(
        "sig-1",
        open_fill(fee="0.1"),
        stop_price=Decimal("95"),
        target_price=Decimal("115"),
    )
    assert state.cash == Decimal("899.9")
    assert "BTC/USDT" in state.positions

    state.apply_close("sig-1", close_fill(fee="0.11"))

    assert state.cash == Decimal("1009.79")
    assert state.realized_pnl_today == Decimal("9.79")
    assert state.positions == {}


def test_open_rejects_duplicate_symbol() -> None:
    state = PortfolioState.new(Decimal("1000"), NOW_MS)
    state.apply_open("sig-1", open_fill(), stop_price=Decimal("95"), target_price=Decimal("115"))
    with pytest.raises(ValueError):
        state.apply_open(
            "sig-2", open_fill(), stop_price=Decimal("95"), target_price=Decimal("115")
        )


def test_open_requires_stop_below_fill_price() -> None:
    state = PortfolioState.new(Decimal("1000"), NOW_MS)
    with pytest.raises(ValueError):
        state.apply_open(
            "sig-1",
            open_fill(price="100"),
            stop_price=Decimal("101"),
            target_price=Decimal("115"),
        )


def test_close_requires_full_quantity() -> None:
    state = PortfolioState.new(Decimal("1000"), NOW_MS)
    state.apply_open("sig-1", open_fill(), stop_price=Decimal("95"), target_price=Decimal("115"))
    with pytest.raises(ValueError):
        state.apply_close("sig-1", close_fill(quantity="0.5"))


def test_resume_requires_exact_phrase() -> None:
    state = PortfolioState.new(Decimal("1000"), NOW_MS)
    state.engage_kill("manual", NOW_MS, "owner")
    with pytest.raises(ValueError):
        state.resume("resume trading")
    state.resume(RESUME_PHRASE)
    assert state.kill_switch is None


def test_manual_kill_does_not_replace_an_existing_reason() -> None:
    state = PortfolioState.new(Decimal("1000"), NOW_MS)
    state.engage_kill("first", NOW_MS, "owner")
    state.engage_kill("second", NOW_MS + 1, "owner")
    assert state.kill_switch is not None
    assert state.kill_switch.reason == "first"


def test_kill_switch_dataclass_can_be_compared() -> None:
    kill = KillSwitch("manual", NOW_MS, "owner")
    assert kill == KillSwitch("manual", NOW_MS, "owner")
