from __future__ import annotations

from decimal import Decimal

import pytest

from nomicronix.execution.models import (
    ExchangeError,
    IntentState,
    OrderStatus,
    ReconciliationError,
)
from tests.execution.fakes import NOW_MS, make_intent, make_order
from tests.execution.harness import build, live_config
from tests.signal_delivery.fakes import make_candidate


def filled_harness():
    harness = build(config=live_config())
    signal = make_candidate()
    intent = harness.service.shadow(signal, now_ms=NOW_MS)
    harness.service.submit(intent.intent_id, "PLACE ORDER", live=True)
    return harness, signal, intent


def test_a_clean_book_reconciles_without_journaling() -> None:
    harness, _, _ = filled_harness()
    try:
        harness.adapter.balance_map = {
            "BTC": Decimal("1"),
            "USDT": Decimal("899"),
        }
        before = harness.store.event_count()
        report = harness.service.reconcile()
        assert report.clean is True
        assert report.matched == ()
        assert harness.store.event_count() == before
    finally:
        harness.close()


def test_a_matched_open_submission_is_clean() -> None:
    harness = build(config=live_config(order_timeout_ms=3, poll_interval_ms=1))
    try:
        signal = make_candidate()
        intent = harness.service.shadow(signal, now_ms=NOW_MS)
        harness.adapter.queue(
            intent.intent_id,
            make_order(
                client_order_id=intent.intent_id,
                status=OrderStatus.OPEN,
                raw_status="NEW",
            ),
        )
        result = harness.service.submit(intent.intent_id, "PLACE ORDER", live=True)
        assert result.intent.state is IntentState.SUBMITTED

        report = harness.service.reconcile()
        assert report.clean is True
        assert report.matched == (intent.intent_id,)
    finally:
        harness.close()


def test_a_missing_local_order_is_reported() -> None:
    harness = build(config=live_config())
    try:
        missing = make_intent(
            signal_id="sig-missing",
            state=IntentState.SUBMITTED,
            exchange_order_id="ex-missing",
        )
        harness.store.create_intent(missing)

        report = harness.service.reconcile()
        assert report.clean is False
        assert report.missing_orders == (missing.intent_id,)
        assert any("missing_order" in line for line in report.detail)
    finally:
        harness.close()


def test_a_submitted_intent_without_an_exchange_id_is_attached_by_client_id() -> None:
    harness = build(config=live_config())
    try:
        intent = make_intent(signal_id="sig-reattach", state=IntentState.SUBMITTED)
        harness.store.create_intent(intent)
        order = make_order(
            client_order_id=intent.intent_id,
            exchange_order_id="ex-reattach",
            status=OrderStatus.OPEN,
        )
        harness.adapter.orders[intent.intent_id] = order
        harness.adapter.by_exchange_id["ex-reattach"] = order

        report = harness.service.reconcile()
        assert report.clean is True
        assert report.matched == (intent.intent_id,)
        stored = harness.store.get_intent(intent.intent_id)
        assert stored is not None
        assert stored.exchange_order_id == "ex-reattach"
        events = harness.store.events_for(intent.intent_id)
        assert [str(event.event_type) for event in events] == ["reconciled"]
        assert events[0].payload == {"attached_exchange_order_id": "ex-reattach"}
    finally:
        harness.close()


def test_an_unknown_exchange_order_is_reported_and_journaled() -> None:
    harness = build(config=live_config())
    try:
        ghost = make_order(
            client_order_id="nx-ghost",
            exchange_order_id="ex-ghost",
            status=OrderStatus.OPEN,
        )
        harness.adapter.orders["nx-ghost"] = ghost
        harness.adapter.by_exchange_id["ex-ghost"] = ghost

        report = harness.service.reconcile()
        assert report.clean is False
        assert report.unknown_orders == ("nx-ghost",)
        journaled = harness.store.events_for("nx-ghost")
        assert [str(event.event_type) for event in journaled] == ["reconciled"]
    finally:
        harness.close()


def test_an_unknown_exchange_position_is_reported() -> None:
    harness = build(config=live_config())
    try:
        harness.adapter.balance_map = {"ETH": Decimal("2")}
        report = harness.service.reconcile()
        assert report.clean is False
        assert report.unknown_positions == ("ETH/USDT",)
        assert any("unknown_position" in line for line in report.detail)
    finally:
        harness.close()


def test_a_risk_position_missing_at_the_exchange_is_reported() -> None:
    harness, _, _ = filled_harness()
    try:
        harness.adapter.balance_map = {"BTC": Decimal("0.5")}
        report = harness.service.reconcile()
        assert report.clean is False
        assert report.unknown_positions == ("BTC/USDT",)
        assert any("risk holds 1" in line for line in report.detail)
    finally:
        harness.close()


def test_reconciliation_failure_blocks_submission_with_exit_three() -> None:
    harness = build(config=live_config())
    try:
        awaiting = make_intent(signal_id="sig-2", state=IntentState.AWAITING_APPROVAL)
        harness.store.create_intent(awaiting)
        ghost = make_order(
            client_order_id="nx-ghost",
            exchange_order_id="ex-ghost",
            status=OrderStatus.OPEN,
        )
        harness.adapter.orders["nx-ghost"] = ghost
        harness.adapter.by_exchange_id["ex-ghost"] = ghost

        with pytest.raises(ReconciliationError) as caught:
            harness.service.submit(awaiting.intent_id, "PLACE ORDER", live=True)
        assert caught.value.exit_code == 3
        assert harness.adapter.create_calls == []
    finally:
        harness.close()


def test_a_network_error_during_reconciliation_is_not_swallowed() -> None:
    harness = build(config=live_config())
    try:
        harness.adapter.failures["open_orders"] = ExchangeError("offline", code="network_error")
        with pytest.raises(ExchangeError) as caught:
            harness.service.reconcile()
        assert caught.value.code == "network_error"
    finally:
        harness.close()
