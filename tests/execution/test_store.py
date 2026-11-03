from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from nomicronix.execution.models import (
    EventType,
    IntentState,
    OrderEvent,
    OrderStatus,
    StoreError,
    intent_id_for,
)
from nomicronix.execution.store import ExecutionStore
from tests.execution.fakes import NOW_MS, make_intent, make_order


def created(intent_id: str = intent_id_for("sig-1")) -> OrderEvent:
    return OrderEvent(intent_id, EventType.INTENT_CREATED, NOW_MS, {"source": "test"})


def open_store(tmp_path) -> ExecutionStore:
    return ExecutionStore(tmp_path / "execution.sqlite")


def test_create_intent_journals_the_creation_event(tmp_path) -> None:
    store = open_store(tmp_path)
    try:
        intent = make_intent()
        assert store.create_intent(intent, event=created(intent.intent_id)) is True
        assert store.get_intent(intent.intent_id) == intent
        assert [event.event_type for event in store.events_for(intent.intent_id)] == [
            EventType.INTENT_CREATED
        ]
        assert store.event_count() == 1
    finally:
        store.close()


def test_a_duplicate_intent_is_a_no_op(tmp_path) -> None:
    store = open_store(tmp_path)
    try:
        intent = make_intent()
        assert store.create_intent(intent, event=created(intent.intent_id)) is True
        assert store.create_intent(intent, event=created(intent.intent_id)) is False
        assert store.event_count() == 1
        assert len(store.list_intents()) == 1
    finally:
        store.close()


def test_intents_and_events_survive_a_reopen(tmp_path) -> None:
    path = tmp_path / "execution.sqlite"
    store = ExecutionStore(path)
    try:
        intent = make_intent()
        store.create_intent(intent, event=created(intent.intent_id))
        store.update_intent(
            intent.intent_id,
            state=IntentState.AWAITING_APPROVAL,
            event=OrderEvent(intent.intent_id, EventType.SHADOWED, NOW_MS + 1, {"ask": "100.5"}),
        )
    finally:
        store.close()

    reopened = ExecutionStore(path)
    try:
        loaded = reopened.get_intent(intent.intent_id)
        assert loaded is not None
        assert loaded.state is IntentState.AWAITING_APPROVAL
        assert [event.event_type for event in reopened.events_for(intent.intent_id)] == [
            EventType.INTENT_CREATED,
            EventType.SHADOWED,
        ]
        assert reopened.events_for(intent.intent_id)[1].payload == {"ask": "100.5"}
    finally:
        reopened.close()


def test_update_is_atomic_with_its_event(tmp_path) -> None:
    store = open_store(tmp_path)
    try:
        intent = make_intent()
        store.create_intent(intent, event=created(intent.intent_id))
        with pytest.raises(StoreError):
            store.update_intent(
                "nx-missing",
                state=IntentState.SUBMITTED,
                event=OrderEvent("nx-missing", EventType.SUBMITTED, NOW_MS),
            )
        assert store.event_count() == 1
    finally:
        store.close()


def test_unknown_persisted_state_is_a_store_error(tmp_path) -> None:
    path = tmp_path / "execution.sqlite"
    store = ExecutionStore(path)
    store.create_intent(make_intent())
    store.close()

    with sqlite3.connect(str(path)) as conn:
        conn.execute("UPDATE intents SET state = 'teleported'")
    reopened = ExecutionStore(path)
    try:
        with pytest.raises(StoreError, match="unknown state"):
            reopened.list_intents()
    finally:
        reopened.close()


def test_unknown_event_type_is_a_store_error(tmp_path) -> None:
    path = tmp_path / "execution.sqlite"
    store = ExecutionStore(path)
    store.create_intent(make_intent(), event=created())
    store.close()

    with sqlite3.connect(str(path)) as conn:
        conn.execute("UPDATE events SET event_type = 'teleported'")
    reopened = ExecutionStore(path)
    try:
        with pytest.raises(StoreError, match="unknown type"):
            reopened.events_for(intent_id_for("sig-1"))
    finally:
        reopened.close()


def test_corrupt_decimal_is_a_store_error(tmp_path) -> None:
    path = tmp_path / "execution.sqlite"
    store = ExecutionStore(path)
    store.create_intent(make_intent())
    store.close()

    with sqlite3.connect(str(path)) as conn:
        conn.execute("UPDATE intents SET quantity = 'not-a-number'")
    reopened = ExecutionStore(path)
    try:
        with pytest.raises(StoreError, match="corrupt"):
            reopened.list_intents()
    finally:
        reopened.close()


def test_list_intents_can_filter_by_state(tmp_path) -> None:
    store = open_store(tmp_path)
    try:
        shadow = make_intent(signal_id="sig-1", state=IntentState.SHADOW)
        approved = make_intent(signal_id="sig-2", state=IntentState.AWAITING_APPROVAL)
        store.create_intent(shadow)
        store.create_intent(approved)

        assert store.list_intents([IntentState.SHADOW]) == [shadow]
        assert len(store.list_intents()) == 2
    finally:
        store.close()


def test_orders_round_trip_and_open_filter(tmp_path) -> None:
    store = open_store(tmp_path)
    try:
        opened = make_order()
        filled = make_order(
            client_order_id=intent_id_for("sig-2"),
            exchange_order_id="ex-2",
            status=OrderStatus.CLOSED,
            filled_quantity="1",
            average_price="101",
            fee="0.1",
            raw_status="closed",
        )
        store.put_order(opened)
        store.put_order(filled)

        assert store.get_order(opened.client_order_id) == opened
        assert [order.client_order_id for order in store.open_orders()] == [opened.client_order_id]
        assert len(store.list_orders()) == 2

        store.put_order(
            make_order(
                status=OrderStatus.CLOSED,
                filled_quantity="1",
                average_price="100",
                raw_status="closed",
            )
        )
        assert store.open_orders() == []
    finally:
        store.close()


def test_intent_id_must_be_derived_from_the_signal_id() -> None:
    with pytest.raises(ValueError, match="derived"):
        make_intent().__class__(
            intent_id="nx-other",
            signal_id="sig-1",
            symbol="BTC/USDT",
            side="buy",
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
            target_price=Decimal("115"),
            approved_notional=Decimal("100"),
            created_at_ms=NOW_MS,
            expires_at_ms=NOW_MS + 1,
            state=IntentState.SHADOW,
        )
