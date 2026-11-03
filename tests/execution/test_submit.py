from __future__ import annotations

from decimal import Decimal

import pytest

from nomicronix.execution.config import ExecutionConfig
from nomicronix.execution.models import (
    ExchangeError,
    IntentError,
    IntentState,
    LiveDisabled,
    OrderStatus,
    ReconciliationError,
    RiskRejected,
    intent_id_for,
)
from tests.execution.fakes import NOW_MS, make_intent, make_order
from tests.execution.harness import build, live_config
from tests.signal_delivery.fakes import make_candidate

PHRASE = "PLACE ORDER"


def approved_harness(**config: object):
    harness = build(config=live_config(**config))
    signal = make_candidate()
    intent = harness.service.shadow(signal, now_ms=NOW_MS)
    assert intent.state is IntentState.AWAITING_APPROVAL
    return harness, signal, intent


def test_wrong_phrase_is_an_input_error_and_reaches_no_exchange() -> None:
    harness, _, intent = approved_harness()
    try:
        with pytest.raises(IntentError, match="exact phrase"):
            harness.service.submit(intent.intent_id, "place order", live=True)
        assert harness.adapter.create_calls == []
        assert harness.store.get_intent(intent.intent_id) == intent
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("config", "live"),
    [
        (ExecutionConfig(dry_run=True, allow_live=False, stage="shadow"), True),
        (ExecutionConfig(dry_run=True), True),
        (ExecutionConfig(dry_run=True, allow_live=True), True),
        (ExecutionConfig(dry_run=False, allow_live=True, stage="shadow"), True),
        (live_config(), False),
    ],
)
def test_each_missing_live_gate_refuses_before_the_exchange(
    config: ExecutionConfig, live: bool
) -> None:
    harness = build(config=config)
    try:
        signal = make_candidate()
        intent = harness.service.shadow(signal, now_ms=NOW_MS)
        with pytest.raises(LiveDisabled) as caught:
            harness.service.submit(intent.intent_id, PHRASE, live=live)
        assert caught.value.exit_code == 4
        assert harness.adapter.create_calls == []
        refusal = [
            event
            for event in harness.store.events_for(intent.intent_id)
            if str(event.event_type) == "refused"
        ]
        assert refusal
    finally:
        harness.close()


def test_a_fully_filled_market_order_is_journaled_once() -> None:
    harness, signal, intent = approved_harness()
    try:
        result = harness.service.submit(intent.intent_id, PHRASE, live=True)

        assert result.timed_out is False
        assert result.intent.state is IntentState.FILLED
        assert result.intent.filled_quantity == Decimal("1")
        assert result.intent.average_fill_price == Decimal("100")
        assert harness.adapter.create_calls == [("BTC/USDT", "buy", Decimal("1"), intent.intent_id)]
        stored = harness.store.get_intent(intent.intent_id)
        assert stored is not None
        assert stored.state is IntentState.FILLED
        events = [event.event_type for event in harness.store.events_for(intent.intent_id)]
        assert [str(event) for event in events] == [
            "approved",
            "submitted",
            "filled",
        ]
        positions = harness.risk.positions()
        assert len(positions) == 1
        assert positions[0].symbol == signal.symbol
        assert positions[0].quantity == Decimal("1")
    finally:
        harness.close()


def test_a_duplicate_submit_never_creates_a_second_order() -> None:
    harness, _, intent = approved_harness()
    try:
        harness.service.submit(intent.intent_id, PHRASE, live=True)
        again = harness.service.submit(intent.intent_id, PHRASE, live=True)
        assert again.intent.state is IntentState.FILLED
        assert len(harness.adapter.create_calls) == 1
        assert len([order for order in harness.store.list_orders()]) == 1
    finally:
        harness.close()


def test_kill_switch_refuses_submission() -> None:
    harness, _, intent = approved_harness()
    try:
        harness.risk.engage_kill_switch("owner test", now_ms=NOW_MS)
        with pytest.raises(RiskRejected) as caught:
            harness.service.submit(intent.intent_id, PHRASE, live=True)
        assert caught.value.code == "kill_switch"
        assert caught.value.exit_code == 2
        assert harness.adapter.create_calls == []
        stored = harness.store.get_intent(intent.intent_id)
        assert stored is not None and stored.state is IntentState.AWAITING_APPROVAL
    finally:
        harness.close()


def test_a_duplicate_symbol_position_refuses_submission() -> None:
    harness, _, first = approved_harness()
    try:
        harness.service.submit(first.intent_id, PHRASE, live=True)
        second = make_intent(signal_id="sig-2", state=IntentState.AWAITING_APPROVAL)
        harness.store.create_intent(second)

        with pytest.raises(RiskRejected) as caught:
            harness.service.submit(second.intent_id, PHRASE, live=True)
        assert caught.value.code == "duplicate_position"
        assert len(harness.adapter.create_calls) == 1
    finally:
        harness.close()


def test_slippage_beyond_the_ceiling_rejects_the_intent() -> None:
    harness, _, intent = approved_harness()
    try:
        harness.adapter.set_ticker("BTC/USDT", bid="100.9", ask="101", at_ms=NOW_MS)
        with pytest.raises(RiskRejected) as caught:
            harness.service.submit(intent.intent_id, PHRASE, live=True)
        assert caught.value.code == "slippage"
        assert harness.adapter.create_calls == []
        stored = harness.store.get_intent(intent.intent_id)
        assert stored is not None and stored.state is IntentState.REJECTED
    finally:
        harness.close()


def test_an_expired_intent_is_rejected_before_any_exchange_call() -> None:
    harness = build(config=live_config(), now_ms=NOW_MS + 300_000)
    try:
        signal = make_candidate()
        intent = harness.service.shadow(signal, now_ms=NOW_MS)
        with pytest.raises(RiskRejected) as caught:
            harness.service.submit(intent.intent_id, PHRASE, live=True)
        assert caught.value.code == "expired"
        assert harness.adapter.create_calls == []
    finally:
        harness.close()


def test_a_stale_intent_is_rejected_before_any_exchange_call() -> None:
    harness = build(config=live_config(max_intent_age_ms=1_000), now_ms=NOW_MS + 1_001)
    try:
        signal = make_candidate()
        intent = harness.service.shadow(signal, now_ms=NOW_MS)
        with pytest.raises(RiskRejected) as caught:
            harness.service.submit(intent.intent_id, PHRASE, live=True)
        assert caught.value.code == "stale_intent"
        assert harness.adapter.create_calls == []
    finally:
        harness.close()


def test_unclean_reconciliation_blocks_submission() -> None:
    harness, _, intent = approved_harness()
    try:
        ghost = make_order(
            client_order_id="nx-ghost",
            exchange_order_id="ex-ghost",
            status=OrderStatus.OPEN,
        )
        harness.adapter.orders["nx-ghost"] = ghost
        harness.adapter.by_exchange_id["ex-ghost"] = ghost

        with pytest.raises(ReconciliationError) as caught:
            harness.service.submit(intent.intent_id, PHRASE, live=True)
        assert caught.value.code == "reconciliation_error"
        assert caught.value.exit_code == 3
        assert harness.adapter.create_calls == []
    finally:
        harness.close()


def test_an_explicit_reconciliation_failure_is_reported_as_exit_three() -> None:
    harness, _, intent = approved_harness()
    try:
        harness.adapter.failures["open_orders"] = ExchangeError(
            "socket closed", code="network_error"
        )
        with pytest.raises(ExchangeError) as caught:
            harness.service.submit(intent.intent_id, PHRASE, live=True)
        assert caught.value.exit_code == 3
        assert harness.adapter.create_calls == []
    finally:
        harness.close()


def test_an_open_order_leaves_the_intent_submitted_and_is_never_resubmitted() -> None:
    harness, _, intent = approved_harness(order_timeout_ms=3, poll_interval_ms=1)
    try:
        opened = make_order(
            client_order_id=intent.intent_id,
            status=OrderStatus.OPEN,
            raw_status="NEW",
        )
        harness.adapter.queue(intent.intent_id, opened)

        first = harness.service.submit(intent.intent_id, PHRASE, live=True)
        assert first.timed_out is True
        assert first.intent.state is IntentState.SUBMITTED
        assert len(harness.adapter.create_calls) == 1

        second = harness.service.submit(intent.intent_id, PHRASE, live=True)
        assert second.timed_out is True
        assert second.intent.state is IntentState.SUBMITTED
        assert len(harness.adapter.create_calls) == 1
    finally:
        harness.close()


def test_an_exchange_rejection_fails_the_intent() -> None:
    harness, _, intent = approved_harness()
    try:
        harness.adapter.failures["create_market_order"] = ExchangeError(
            "rejected", code="exchange_rejected"
        )
        with pytest.raises(ExchangeError):
            harness.service.submit(intent.intent_id, PHRASE, live=True)
        stored = harness.store.get_intent(intent.intent_id)
        assert stored is not None and stored.state is IntentState.FAILED
        assert [str(event.event_type) for event in harness.store.events_for(intent.intent_id)][
            -1
        ] == "failed"
    finally:
        harness.close()


def test_an_unknown_submit_outcome_stays_submitted_for_reconciliation() -> None:
    harness, _, intent = approved_harness()
    try:
        harness.adapter.failures["create_market_order"] = ExchangeError(
            "socket closed", code="network_error"
        )
        with pytest.raises(ExchangeError, match="run reconcile"):
            harness.service.submit(intent.intent_id, PHRASE, live=True)
        stored = harness.store.get_intent(intent.intent_id)
        assert stored is not None and stored.state is IntentState.SUBMITTED
        assert harness.adapter.create_calls == []
    finally:
        harness.close()


def test_a_shadow_intent_is_not_submittable() -> None:
    harness = build(config=live_config())
    try:
        intent = harness.store.get_intent(intent_id_for("sig-x"))
        assert intent is None
        shadow = make_intent(signal_id="sig-x", state=IntentState.SHADOW)
        harness.store.create_intent(shadow)
        with pytest.raises(IntentError, match="cannot be submitted"):
            harness.service.submit(shadow.intent_id, PHRASE, live=True)
        assert harness.adapter.create_calls == []
    finally:
        harness.close()


def test_an_unknown_intent_is_an_input_error() -> None:
    harness = build(config=live_config())
    try:
        with pytest.raises(IntentError, match="unknown intent"):
            harness.service.submit("nx-missing", PHRASE, live=True)
    finally:
        harness.close()


def test_reconciliation_error_without_a_reconciler_is_explicit() -> None:
    from nomicronix.execution.submit import Submitter

    harness, _, intent = approved_harness()
    try:
        submitter = Submitter(
            harness.risk,
            harness.store,
            harness.adapter,
            harness.config,
            clock=lambda: NOW_MS,
            sleep=lambda _seconds: None,
        )
        with pytest.raises(ReconciliationError):
            submitter.submit(intent.intent_id, PHRASE, live=True)
    finally:
        harness.close()
