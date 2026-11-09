from __future__ import annotations

from decimal import Decimal

import pytest

from nomicronix.execution.models import (
    ExchangeError,
    IntentError,
    IntentState,
    OrderStatus,
    intent_id_for,
)
from tests.execution.fakes import NOW_MS, make_intent, make_order
from tests.execution.harness import build, live_config
from tests.signal_delivery.fakes import make_candidate


def filled_harness():
    harness = build(config=live_config())
    signal = make_candidate()
    intent = harness.service.shadow(signal, now_ms=NOW_MS)
    harness.service.submit(intent.intent_id, "PLACE ORDER", live=True)
    harness.adapter.balance_map = {"BTC": Decimal("1")}
    return harness, signal, intent


def test_flatten_requires_the_exact_phrase() -> None:
    harness, _, _ = filled_harness()
    try:
        with pytest.raises(IntentError, match="FLATTEN NOW"):
            harness.service.flatten("flatten now")
        assert len(harness.risk.positions()) == 1
    finally:
        harness.close()


def test_flatten_cancels_an_open_order_and_expires_the_remainder() -> None:
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

        flattened = harness.service.flatten("FLATTEN NOW")

        assert flattened.canceled == (intent.intent_id,)
        assert flattened.closed == ()
        stored = harness.store.get_intent(intent.intent_id)
        assert stored is not None and stored.state is IntentState.CANCELED
        approval = harness.risk_store.load_approval(signal.signal_id)
        assert approval is not None and approval.consumed_at_ms is not None
        assert harness.risk.positions() == ()
    finally:
        harness.close()


def test_flatten_sells_the_open_position_and_reports_the_close() -> None:
    harness, signal, intent = filled_harness()
    try:
        flattened = harness.service.flatten("FLATTEN NOW")

        assert flattened.canceled == ()
        assert len(flattened.closed) == 1
        closed = flattened.closed[0]
        assert closed.signal_id == signal.signal_id
        assert closed.filled_quantity == Decimal("1")
        assert closed.average_price == Decimal("100")
        assert harness.risk.positions() == ()
        assert harness.adapter.create_calls[-1] == (
            signal.symbol,
            "sell",
            Decimal("1"),
            intent_id_for(f"flatten:{signal.signal_id}"),
        )
        events = harness.store.events_for(intent.intent_id)
        assert str(events[-1].event_type) == "flattened"
    finally:
        harness.close()


def test_flatten_works_while_the_kill_switch_is_engaged() -> None:
    harness, _, _ = filled_harness()
    try:
        harness.risk.engage_kill_switch("owner panic", now_ms=NOW_MS)
        flattened = harness.service.flatten("FLATTEN NOW")
        assert len(flattened.closed) == 1
        assert harness.risk.positions() == ()
    finally:
        harness.close()


def test_a_partial_flatten_leaves_the_remainder_open() -> None:
    harness, signal, _ = filled_harness()
    try:
        client_id = intent_id_for(f"flatten:{signal.signal_id}")
        harness.adapter.queue(
            client_id,
            make_order(
                client_order_id=client_id,
                exchange_order_id="ex-flat-1",
                side="sell",
                status=OrderStatus.CANCELED,
                filled_quantity="0.5",
                average_price="100",
                raw_status="CANCELED",
            ),
        )

        flattened = harness.service.flatten("FLATTEN NOW")

        assert flattened.closed[0].filled_quantity == Decimal("0.5")
        positions = harness.risk.positions()
        assert len(positions) == 1
        assert positions[0].quantity == Decimal("0.5")
    finally:
        harness.close()


def test_a_paper_position_without_exchange_balance_is_skipped() -> None:
    harness, signal, _ = filled_harness()
    try:
        harness.adapter.balance_map = {}
        before = len(harness.adapter.create_calls)
        flattened = harness.service.flatten("FLATTEN NOW")

        assert flattened.closed == ()
        assert flattened.skipped == (signal.signal_id,)
        assert len(harness.adapter.create_calls) == before
        assert len(harness.risk.positions()) == 1
        events = harness.store.events_for(intent_id_for(signal.signal_id))
        assert events[-1].payload["reason"] == "no_exchange_balance"
    finally:
        harness.close()


def test_a_flatten_sell_is_capped_to_the_exchange_balance() -> None:
    harness, signal, _ = filled_harness()
    try:
        harness.adapter.balance_map = {"BTC": Decimal("0.4")}
        flattened = harness.service.flatten("FLATTEN NOW")

        assert len(flattened.closed) == 1
        assert flattened.closed[0].filled_quantity == Decimal("0.4")
        assert flattened.closed[0].requested_quantity == Decimal("1")
        assert harness.adapter.create_calls[-1][2] == Decimal("0.4")
        positions = harness.risk.positions()
        assert len(positions) == 1
        assert positions[0].quantity == Decimal("0.6")
    finally:
        harness.close()


def test_flatten_is_empty_when_there_is_nothing_to_do() -> None:
    harness = build(config=live_config())
    try:
        flattened = harness.service.flatten("FLATTEN NOW")
        assert flattened.canceled == ()
        assert flattened.closed == ()
        assert flattened.timed_out == ()
    finally:
        harness.close()


def test_flatten_journals_an_unknown_exchange_order() -> None:
    harness = build(config=live_config())
    try:
        ghost = make_order(
            client_order_id="nx-ghost",
            exchange_order_id="ex-ghost",
            status=OrderStatus.OPEN,
        )
        harness.adapter.orders["nx-ghost"] = ghost
        harness.adapter.by_exchange_id["ex-ghost"] = ghost

        flattened = harness.service.flatten("FLATTEN NOW")

        assert flattened.canceled == ("nx-ghost",)
        events = harness.store.events_for("nx-unknown-nx-ghost")
        assert [str(event.event_type) for event in events] == ["flattened"]
    finally:
        harness.close()


def test_a_flatten_sell_that_never_terminal_finishes_reports_it() -> None:
    harness, signal, _ = filled_harness()
    try:
        client_id = intent_id_for(f"flatten:{signal.signal_id}")
        harness.adapter.queue(
            client_id,
            make_order(client_order_id=client_id, side="sell", status=OrderStatus.OPEN),
        )
        flattened = harness.service.flatten("FLATTEN NOW")
        assert flattened.timed_out == (signal.signal_id,)
        assert flattened.closed[0].filled_quantity == Decimal("0")
        assert len(harness.risk.positions()) == 1
    finally:
        harness.close()


def test_a_flatten_sell_cannot_fill_more_than_the_position() -> None:
    harness, signal, _ = filled_harness()
    try:
        client_id = intent_id_for(f"flatten:{signal.signal_id}")
        harness.adapter.queue(
            client_id,
            make_order(
                client_order_id=client_id,
                side="sell",
                status=OrderStatus.CLOSED,
                quantity="2",
                filled_quantity="2",
                average_price="100",
            ),
        )
        with pytest.raises(ExchangeError, match="more than the open position"):
            harness.service.flatten("FLATTEN NOW")
        assert len(harness.risk.positions()) == 1
    finally:
        harness.close()


def test_cancel_refuses_an_unknown_terminal_or_unjournaled_intent() -> None:
    harness, _, intent = filled_harness()
    try:
        with pytest.raises(IntentError, match="unknown intent"):
            harness.service.cancel("nx-missing")
        with pytest.raises(IntentError, match="cannot be canceled"):
            harness.service.cancel(intent.intent_id)

        awaiting = make_intent(signal_id="sig-await", state=IntentState.AWAITING_APPROVAL)
        harness.store.create_intent(awaiting)
        with pytest.raises(IntentError, match="no exchange order"):
            harness.service.cancel(awaiting.intent_id)
    finally:
        harness.close()


def test_cancel_closes_one_open_order() -> None:
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
        harness.service.submit(intent.intent_id, "PLACE ORDER", live=True)

        canceled = harness.service.cancel(intent.intent_id)

        assert canceled.state is IntentState.CANCELED
        approval = harness.risk_store.load_approval(signal.signal_id)
        assert approval is not None and approval.consumed_at_ms is not None
    finally:
        harness.close()


def test_service_properties_expose_the_wiring() -> None:
    harness = build(config=live_config())
    try:
        assert harness.service.config is harness.config
        assert harness.service.store is harness.store
        assert harness.service.adapter is harness.adapter
        assert harness.service.risk is harness.risk
    finally:
        harness.close()
