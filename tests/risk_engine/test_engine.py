from decimal import Decimal

from nomicronix.risk_engine.engine import evaluate
from nomicronix.risk_engine.models import KillSwitch, RejectReason
from tests.risk_engine.fakes import (
    HOUR_MS,
    NOW_MS,
    make_instrument,
    make_limits,
    make_position,
    make_proposal,
    make_snapshot,
)


def evaluate_default(**overrides: object):
    proposal = overrides.pop("proposal", make_proposal())
    snapshot = overrides.pop("snapshot", make_snapshot())
    instrument = overrides.pop("instrument", make_instrument())
    limits = overrides.pop("limits", make_limits())
    now_ms = int(overrides.pop("now_ms", NOW_MS))
    return evaluate(proposal, snapshot, instrument, limits, now_ms)  # type: ignore[arg-type]


def test_conservative_fixture_is_approved() -> None:
    decision = evaluate_default()
    assert decision.approved
    assert decision.reasons == ()
    assert decision.approved_quantity == Decimal("1")
    assert decision.approved_notional == Decimal("100")
    assert decision.risk_amount == Decimal("5")
    assert decision.reward_risk == Decimal("3")


def test_kill_switch_rejects_everything() -> None:
    kill = KillSwitch("manual", NOW_MS, "owner")
    decision = evaluate_default(snapshot=make_snapshot(kill_switch=kill))
    assert not decision.approved
    assert RejectReason.KILL_SWITCH in decision.reasons


def test_stale_signal_is_rejected() -> None:
    decision = evaluate_default(
        proposal=make_proposal(created_at_ms=NOW_MS - 5 * 60_000 - 1),
        snapshot=make_snapshot(as_of_ms=NOW_MS),
    )
    assert RejectReason.STALE_SIGNAL in decision.reasons


def test_future_signal_is_rejected() -> None:
    decision = evaluate_default(proposal=make_proposal(created_at_ms=NOW_MS + 1))
    assert RejectReason.STALE_SIGNAL in decision.reasons


def test_stale_state_is_rejected() -> None:
    decision = evaluate_default(
        proposal=make_proposal(created_at_ms=NOW_MS),
        snapshot=make_snapshot(as_of_ms=NOW_MS - 5 * 60_000 - 1),
    )
    assert RejectReason.STALE_STATE in decision.reasons


def test_symbol_allowlist_is_enforced() -> None:
    decision = evaluate_default(
        proposal=make_proposal(symbol="SOL/USDT"),
        instrument=make_instrument("SOL/USDT"),
    )
    assert RejectReason.SYMBOL_NOT_ALLOWED in decision.reasons


def test_instrument_must_match_symbol() -> None:
    decision = evaluate_default(instrument=make_instrument("ETH/USDT"))
    assert RejectReason.INSTRUMENT_MISMATCH in decision.reasons


def test_invalid_proposal_returns_zeros() -> None:
    decision = evaluate_default(proposal=make_proposal(stop="105"))
    assert not decision.approved
    assert RejectReason.INVALID_PROPOSAL in decision.reasons
    assert decision.approved_quantity == Decimal(0)
    assert decision.approved_notional == Decimal(0)
    assert decision.risk_amount == Decimal(0)


def test_duplicate_symbol_is_rejected() -> None:
    decision = evaluate_default(
        snapshot=make_snapshot(positions=(make_position(),)),
    )
    assert RejectReason.DUPLICATE_POSITION in decision.reasons


def test_max_positions_is_enforced() -> None:
    positions = (
        make_position(symbol="BTC/USDT", signal_id="1", stop="100"),
        make_position(symbol="ETH/USDT", signal_id="2", stop="100"),
    )
    decision = evaluate_default(snapshot=make_snapshot(positions=positions))
    assert RejectReason.MAX_POSITIONS in decision.reasons


def test_daily_loss_limit_is_enforced() -> None:
    decision = evaluate_default(
        snapshot=make_snapshot(equity="970", day_start="1000", peak="1000"),
    )
    assert RejectReason.DAILY_LOSS_LIMIT in decision.reasons


def test_max_drawdown_is_enforced() -> None:
    decision = evaluate_default(
        snapshot=make_snapshot(equity="900", day_start="900", peak="1000"),
    )
    assert RejectReason.MAX_DRAWDOWN in decision.reasons


def test_reward_risk_is_enforced() -> None:
    decision = evaluate_default(proposal=make_proposal(target="105"))
    assert RejectReason.REWARD_RISK_TOO_LOW in decision.reasons


def test_size_below_minimum_is_rejected() -> None:
    decision = evaluate_default(
        snapshot=make_snapshot(equity="50", day_start="50", peak="50"),
    )
    assert RejectReason.SIZE_BELOW_MINIMUM in decision.reasons


def test_total_exposure_cap_is_enforced() -> None:
    position = make_position(quantity="4.5", entry="100", stop="100", current="100")
    decision = evaluate_default(snapshot=make_snapshot(positions=(position,)))
    assert RejectReason.MAX_TOTAL_EXPOSURE in decision.reasons


def test_open_risk_cap_is_enforced() -> None:
    position = make_position(quantity="4", entry="100", stop="95", current="100")
    decision = evaluate_default(snapshot=make_snapshot(positions=(position,)))
    assert RejectReason.MAX_OPEN_RISK in decision.reasons


def test_reasons_have_a_stable_order() -> None:
    kill = KillSwitch("manual", NOW_MS, "owner")
    decision = evaluate_default(
        proposal=make_proposal(symbol="SOL/USDT", created_at_ms=NOW_MS - HOUR_MS),
        instrument=make_instrument("ETH/USDT"),
        snapshot=make_snapshot(kill_switch=kill, as_of_ms=NOW_MS - HOUR_MS),
    )
    assert decision.reasons[:5] == (
        RejectReason.KILL_SWITCH,
        RejectReason.STALE_SIGNAL,
        RejectReason.STALE_STATE,
        RejectReason.SYMBOL_NOT_ALLOWED,
        RejectReason.INSTRUMENT_MISMATCH,
    )


def test_same_inputs_produce_identical_decisions() -> None:
    first = evaluate_default()
    second = evaluate_default()
    assert first == second


def test_notional_cap_is_explained_in_trace() -> None:
    decision = evaluate_default(proposal=make_proposal(entry="10", stop="9.9", target="10.2"))
    assert decision.approved
    assert "capped_by_notional=True" in decision.rule_trace
