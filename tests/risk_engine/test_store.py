import json
from decimal import Decimal

import pytest

from nomicronix.risk_engine.models import (
    Approval,
    Fill,
    FillAction,
    RejectReason,
    RiskLimits,
)
from nomicronix.risk_engine.service import RiskService
from nomicronix.risk_engine.state import PortfolioState
from nomicronix.risk_engine.store import ApprovalError, RiskStateStore
from tests.risk_engine.fakes import NOW_MS, make_instrument, make_proposal


def make_service(path, *, cash: str = "1000") -> tuple[RiskStateStore, RiskService]:
    store = RiskStateStore(path)
    service = RiskService(
        store,
        RiskLimits(),
        initial_cash=Decimal(cash),
        clock=lambda: NOW_MS,
    )
    return store, service


def open_fill(price: str = "100", quantity: str = "1", fee: str = "0") -> Fill:
    return Fill(
        FillAction.OPEN, "BTC/USDT", Decimal(quantity), Decimal(price), Decimal(fee), NOW_MS
    )


def close_fill(price: str = "110", quantity: str = "1", fee: str = "0") -> Fill:
    return Fill(
        FillAction.CLOSE, "BTC/USDT", Decimal(quantity), Decimal(price), Decimal(fee), NOW_MS + 1
    )


def test_check_persists_decision_and_approval(tmp_path) -> None:
    store, service = make_service(str(tmp_path / "risk.sqlite"))
    try:
        decision = service.check(make_proposal(), make_instrument())
        assert decision.approved
        assert store.decision_count() == 1
        approval = store.load_approval("sig-1")
        assert isinstance(approval, Approval)
        assert approval.quantity == Decimal("1")
        assert approval.stop_price == Decimal("95")
        assert approval.target_price == Decimal("115")
        assert approval.consumed_at_ms is None
    finally:
        store.close()


def test_rejected_check_is_journaled_without_an_approval(tmp_path) -> None:
    store, service = make_service(str(tmp_path / "risk.sqlite"))
    try:
        decision = service.check(make_proposal(target="105"), make_instrument())
        assert not decision.approved
        assert store.decision_count() == 1
        assert store.load_approval("sig-1") is None
        rows = store.decisions_for("sig-1")
        payload = json.loads(rows[0]["decision_json"])
        assert "reward_risk_too_low" in payload["reasons"]
    finally:
        store.close()


def test_approval_is_single_use_and_fill_updates_state(tmp_path) -> None:
    store, service = make_service(str(tmp_path / "risk.sqlite"))
    try:
        service.check(make_proposal(), make_instrument())
        state = service.record_fill("sig-1", open_fill(fee="0.1"))
        assert "BTC/USDT" in state.positions
        assert store.load_approval("sig-1").consumed_at_ms is not None

        with pytest.raises(ApprovalError):
            service.record_fill("sig-1", open_fill())

        status = service.status()
        assert status.open_positions == 1

        service.record_fill("sig-1", close_fill(fee="0.11"))
        assert service.status().open_positions == 0
    finally:
        store.close()


def test_unknown_fill_is_rejected(tmp_path) -> None:
    store, service = make_service(str(tmp_path / "risk.sqlite"))
    try:
        with pytest.raises(ApprovalError):
            service.record_fill("missing", open_fill())
    finally:
        store.close()


def test_restart_preserves_kill_switch_and_resume(tmp_path) -> None:
    path = str(tmp_path / "risk.sqlite")
    store, service = make_service(path)
    service.engage_kill_switch("manual stop")
    store.close()

    store2, service2 = make_service(path)
    try:
        status = service2.status()
        assert status.kill_switch is not None
        assert status.trading_allowed is False
        service2.resume("RESUME TRADING")
        assert service2.status().kill_switch is None
    finally:
        store2.close()


def test_drawdown_check_persists_the_kill_switch(tmp_path) -> None:
    path = str(tmp_path / "risk.sqlite")
    store, service = make_service(path)
    try:
        state = PortfolioState.new(Decimal("1000"), NOW_MS)
        state.cash = Decimal("800")
        store.save_state(state, NOW_MS)

        decision = service.check(make_proposal(), make_instrument())

        assert RejectReason.MAX_DRAWDOWN in decision.reasons
        assert service.status().kill_switch is not None
    finally:
        store.close()

    store2, service2 = make_service(path)
    try:
        assert service2.status().kill_switch is not None
    finally:
        store2.close()


def test_stale_state_rejects_a_check(tmp_path) -> None:
    store, service = make_service(str(tmp_path / "risk.sqlite"))
    try:
        state = PortfolioState.new(Decimal("1000"), NOW_MS - 600_000)
        store.save_state(state, NOW_MS - 600_000)
        decision = service.check(make_proposal(), make_instrument())
        assert RejectReason.STALE_STATE in decision.reasons
    finally:
        store.close()


def test_mark_round_trips_through_sqlite(tmp_path) -> None:
    path = str(tmp_path / "risk.sqlite")
    store, service = make_service(path)
    try:
        service.check(make_proposal(), make_instrument())
        service.record_fill("sig-1", open_fill())
        status = service.mark({"BTC/USDT": Decimal("110")})
        assert status.equity == Decimal("1010")
    finally:
        store.close()

    store2, service2 = make_service(path)
    try:
        assert service2.status().equity == Decimal("1010")
    finally:
        store2.close()
