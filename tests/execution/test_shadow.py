from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nomicronix.execution.config import ExecutionConfig
from nomicronix.execution.models import EventType, IntentState
from nomicronix.execution.service import ExecutionService
from nomicronix.execution.store import ExecutionStore
from nomicronix.risk_engine.models import RiskLimits
from nomicronix.risk_engine.service import RiskService
from nomicronix.risk_engine.store import RiskStateStore
from tests.execution.fakes import NOW_MS, FakeAdapter
from tests.signal_delivery.fakes import make_candidate, write_signals


@dataclass
class Harness:
    service: ExecutionService
    store: ExecutionStore
    risk_store: RiskStateStore
    adapter: FakeAdapter
    config: ExecutionConfig

    def close(self) -> None:
        self.store.close()
        self.risk_store.close()


def build(
    *,
    config: ExecutionConfig | None = None,
    limits: RiskLimits | None = None,
) -> Harness:
    active = config if config is not None else ExecutionConfig()
    adapter = FakeAdapter()
    store = ExecutionStore(":memory:")
    risk_store = RiskStateStore(":memory:")
    risk = RiskService(
        risk_store,
        limits if limits is not None else RiskLimits(),
        initial_cash=Decimal("1000"),
        clock=lambda: NOW_MS,
    )
    adapter.set_ticker("BTC/USDT", bid="100", ask="100.1", at_ms=NOW_MS)
    service = ExecutionService(risk, store, adapter, active, clock=lambda: NOW_MS)
    return Harness(service, store, risk_store, adapter, active)


def test_an_approved_signal_becomes_a_shadow_intent() -> None:
    harness = build()
    try:
        signal = make_candidate()
        intent = harness.service.shadow(signal)

        assert intent.intent_id == f"nx-{signal.signal_id}"
        assert intent.signal_id == signal.signal_id
        assert intent.state is IntentState.SHADOW
        assert intent.quantity == Decimal("1")
        assert intent.approved_notional == Decimal("100")
        assert intent.entry_price == Decimal("100")
        assert intent.stop_price == Decimal("95")
        assert intent.target_price == Decimal("115")
        assert harness.store.get_intent(intent.intent_id) == intent
        assert [event.event_type for event in harness.store.events_for(intent.intent_id)] == [
            EventType.SHADOWED
        ]
    finally:
        harness.close()


def test_shadow_makes_no_private_exchange_call() -> None:
    harness = build()
    try:
        harness.service.shadow(make_candidate())
        assert harness.adapter.private_calls == []
        assert harness.adapter.public_calls == ["fetch_ticker:BTC/USDT"]
    finally:
        harness.close()


def test_approved_stage_creates_an_intent_awaiting_approval() -> None:
    harness = build(config=ExecutionConfig(stage="approved"))
    try:
        intent = harness.service.shadow(make_candidate())
        assert intent.state is IntentState.AWAITING_APPROVAL
        assert [event.event_type for event in harness.store.events_for(intent.intent_id)] == [
            EventType.APPROVED
        ]
    finally:
        harness.close()


def test_a_rejected_risk_decision_is_journaled_but_not_submittable() -> None:
    harness = build()
    try:
        signal = make_candidate(target="105")
        intent = harness.service.shadow(signal)

        assert intent.state is IntentState.REJECTED
        assert intent.quantity == Decimal("0")
        assert intent.approved_notional == Decimal("0")
        events = harness.store.events_for(intent.intent_id)
        assert [event.event_type for event in events] == [EventType.REJECTED]
        assert "reward_risk_too_low" in events[0].payload["reasons"]  # type: ignore[operator]
        assert harness.adapter.public_calls == []
    finally:
        harness.close()


def test_an_expired_signal_is_rejected_before_any_network_call() -> None:
    harness = build()
    try:
        signal = make_candidate()
        intent = harness.service.shadow(signal, now_ms=signal.expires_at_ms)

        assert intent.state is IntentState.REJECTED
        assert "expired" in harness.store.events_for(intent.intent_id)[0].payload["reasons"]  # type: ignore[operator]
        assert harness.adapter.public_calls == []
    finally:
        harness.close()


def test_a_stale_intent_is_rejected_before_any_network_call() -> None:
    harness = build(config=ExecutionConfig(max_intent_age_ms=1_000))
    try:
        signal = make_candidate()
        intent = harness.service.shadow(signal, now_ms=signal.created_at_ms + 1_001)

        assert intent.state is IntentState.REJECTED
        assert "stale_intent" in harness.store.events_for(intent.intent_id)[0].payload["reasons"]  # type: ignore[operator]
        assert harness.adapter.public_calls == []
    finally:
        harness.close()


def test_slippage_above_the_ceiling_is_rejected() -> None:
    harness = build(config=ExecutionConfig(max_slippage_bps=50))
    try:
        harness.adapter.set_ticker("BTC/USDT", bid="100.4", ask="100.6", at_ms=NOW_MS)
        intent = harness.service.shadow(make_candidate())

        assert intent.state is IntentState.REJECTED
        payload = harness.store.events_for(intent.intent_id)[0].payload
        assert "slippage" in payload["reasons"]  # type: ignore[operator]
        assert payload["ask"] == "100.6"
    finally:
        harness.close()


def test_slippage_exactly_at_the_ceiling_is_allowed() -> None:
    harness = build(config=ExecutionConfig(max_slippage_bps=50))
    try:
        harness.adapter.set_ticker("BTC/USDT", bid="100.4", ask="100.5", at_ms=NOW_MS)
        intent = harness.service.shadow(make_candidate())
        assert intent.state is IntentState.SHADOW
    finally:
        harness.close()


def test_shadow_is_idempotent_for_the_same_signal() -> None:
    harness = build()
    try:
        signal = make_candidate()
        first = harness.service.shadow(signal)
        second = harness.service.shadow(signal)

        assert first == second
        assert harness.store.event_count() == 1
        assert len(harness.store.list_intents()) == 1
        assert harness.adapter.public_calls == ["fetch_ticker:BTC/USDT"]
    finally:
        harness.close()


def test_shadow_file_loads_every_signal(tmp_path) -> None:
    harness = build()
    try:
        path = write_signals(
            tmp_path / "signals.json",
            [make_candidate(0), make_candidate(1, target="105")],
        )
        intents = harness.service.shadow_file(path)

        assert len(intents) == 2
        assert [intent.state for intent in intents] == [
            IntentState.SHADOW,
            IntentState.REJECTED,
        ]
    finally:
        harness.close()


def test_a_signal_for_an_unallowed_symbol_is_rejected_by_risk() -> None:
    harness = build()
    try:
        signal = make_candidate(symbol="DOGE/USDT")
        intent = harness.service.shadow(signal)

        assert intent.state is IntentState.REJECTED
        assert (
            "symbol_not_allowed" in harness.store.events_for(intent.intent_id)[0].payload["reasons"]
        )  # type: ignore[operator]
    finally:
        harness.close()


def test_an_existing_intent_is_never_overwritten(tmp_path) -> None:
    harness = build()
    try:
        signal = make_candidate()
        first = harness.service.shadow(signal)
        harness.service.shadow(signal, now_ms=signal.created_at_ms + 1)
        assert harness.store.get_intent(first.intent_id) == first
    finally:
        harness.close()
