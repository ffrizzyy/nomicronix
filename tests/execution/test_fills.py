from __future__ import annotations

from decimal import Decimal

import pytest

from nomicronix.execution.models import ExchangeError, IntentState, OrderStatus
from nomicronix.risk_engine.models import Fill, FillAction
from nomicronix.risk_engine.store import ApprovalError
from tests.execution.fakes import make_order
from tests.execution.harness import build, live_config
from tests.signal_delivery.fakes import make_candidate


def awaiting():
    harness = build(config=live_config(order_timeout_ms=10, poll_interval_ms=1))
    signal = make_candidate()
    intent = harness.service.shadow(signal, now_ms=signal.created_at_ms)
    return harness, signal, intent


def test_a_full_fill_reports_the_approved_quantity_once() -> None:
    harness, signal, intent = awaiting()
    try:
        result = harness.service.submit(intent.intent_id, "PLACE ORDER", live=True)
        approval = harness.risk_store.load_approval(signal.signal_id)
        assert result.intent.state is IntentState.FILLED
        assert approval is not None
        assert approval.filled_quantity == Decimal("1")
        assert approval.consumed_at_ms is not None
        assert len(harness.risk.positions()) == 1
        assert harness.risk.positions()[0].quantity == Decimal("1")
    finally:
        harness.close()


def test_partial_pieces_are_aggregated_and_reported_once() -> None:
    harness, signal, intent = awaiting()
    try:
        harness.adapter.queue(
            intent.intent_id,
            make_order(
                client_order_id=intent.intent_id,
                status=OrderStatus.OPEN,
                filled_quantity="0.4",
                average_price="100",
                fee="0.04",
                raw_status="PARTIALLY_FILLED",
            ),
            make_order(
                client_order_id=intent.intent_id,
                status=OrderStatus.CLOSED,
                filled_quantity="1",
                average_price="102",
                fee="0.1",
                raw_status="FILLED",
            ),
        )

        result = harness.service.submit(intent.intent_id, "PLACE ORDER", live=True)

        assert result.intent.state is IntentState.FILLED
        assert result.intent.filled_quantity == Decimal("1")
        assert result.intent.average_fill_price == Decimal("102")
        approval = harness.risk_store.load_approval(signal.signal_id)
        assert approval is not None and approval.consumed_at_ms is not None
        positions = harness.risk.positions()
        assert len(positions) == 1
        assert positions[0].quantity == Decimal("1")
        assert positions[0].entry_price == Decimal("102")
        assert positions[0].entry_fee == Decimal("0.1")
        events = [str(event.event_type) for event in harness.store.events_for(intent.intent_id)]
        assert events[-3:] == ["submitted", "partially_filled", "filled"]
    finally:
        harness.close()


def test_a_canceled_remainder_is_expired_and_never_opens_a_second_position() -> None:
    harness, signal, intent = awaiting()
    try:
        harness.adapter.queue(
            intent.intent_id,
            make_order(
                client_order_id=intent.intent_id,
                status=OrderStatus.CANCELED,
                filled_quantity="0.4",
                average_price="100",
                fee="0.04",
                raw_status="CANCELED",
            ),
        )

        result = harness.service.submit(intent.intent_id, "PLACE ORDER", live=True)

        assert result.intent.state is IntentState.CANCELED
        assert result.intent.filled_quantity == Decimal("0.4")
        positions = harness.risk.positions()
        assert len(positions) == 1
        assert positions[0].quantity == Decimal("0.4")

        approval = harness.risk_store.load_approval(signal.signal_id)
        assert approval is not None
        assert approval.filled_quantity == Decimal("0.4")
        assert approval.consumed_at_ms is not None

        with pytest.raises(ApprovalError, match="already consumed"):
            harness.risk.record_fill(
                signal.signal_id,
                Fill(
                    FillAction.OPEN,
                    signal.symbol,
                    Decimal("0.6"),
                    Decimal("100"),
                    Decimal("0"),
                    signal.created_at_ms,
                ),
                now_ms=signal.created_at_ms,
            )
        assert len(harness.risk.positions()) == 1
    finally:
        harness.close()


def test_a_canceled_order_with_no_fill_expires_the_approval() -> None:
    harness, signal, intent = awaiting()
    try:
        harness.adapter.queue(
            intent.intent_id,
            make_order(
                client_order_id=intent.intent_id,
                status=OrderStatus.CANCELED,
                raw_status="CANCELED",
            ),
        )
        result = harness.service.submit(intent.intent_id, "PLACE ORDER", live=True)
        assert result.intent.state is IntentState.CANCELED
        assert harness.risk.positions() == ()
        approval = harness.risk_store.load_approval(signal.signal_id)
        assert approval is not None and approval.consumed_at_ms is not None
    finally:
        harness.close()


def test_an_exchange_rejection_never_consumes_the_approval() -> None:
    harness, signal, intent = awaiting()
    try:
        harness.adapter.queue(
            intent.intent_id,
            make_order(
                client_order_id=intent.intent_id,
                status=OrderStatus.REJECTED,
                raw_status="REJECTED",
            ),
        )
        result = harness.service.submit(intent.intent_id, "PLACE ORDER", live=True)
        assert result.intent.state is IntentState.REJECTED
        assert harness.risk.positions() == ()
        approval = harness.risk_store.load_approval(signal.signal_id)
        assert approval is not None and approval.consumed_at_ms is not None
    finally:
        harness.close()


def test_an_overfill_is_refused_before_risk_sees_it() -> None:
    harness, _, intent = awaiting()
    try:
        harness.adapter.queue(
            intent.intent_id,
            make_order(
                client_order_id=intent.intent_id,
                quantity="2",
                status=OrderStatus.CLOSED,
                filled_quantity="2",
                average_price="100",
                raw_status="FILLED",
            ),
        )
        with pytest.raises(ExchangeError) as caught:
            harness.service.submit(intent.intent_id, "PLACE ORDER", live=True)
        assert caught.value.code == "overfilled"
        assert harness.risk.positions() == ()
    finally:
        harness.close()
