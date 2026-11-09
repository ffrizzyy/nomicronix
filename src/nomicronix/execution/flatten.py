"""Cancel every open order and close every open position: the panic button.

Flatten only reduces exposure. It works even while the kill switch is engaged
and it never opens, increases or re-sizes a position. Every fill it causes is
reported through ``RiskService.record_fill`` as a CLOSE.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from nomicronix.execution.adapter import ExchangeAdapter
from nomicronix.execution.config import ExecutionConfig
from nomicronix.execution.fills import FillReporter
from nomicronix.execution.models import (
    EventType,
    ExchangeError,
    ExchangeOrder,
    IntentError,
    OrderEvent,
    intent_id_for,
)
from nomicronix.execution.store import ExecutionStore
from nomicronix.risk_engine.models import Fill, FillAction
from nomicronix.risk_engine.service import RiskService

FLATTEN_PHRASE = "FLATTEN NOW"


@dataclass(frozen=True, slots=True)
class ClosedPosition:
    signal_id: str
    symbol: str
    requested_quantity: Decimal
    filled_quantity: Decimal
    average_price: Decimal | None
    fee: Decimal


@dataclass(frozen=True, slots=True)
class FlattenResult:
    canceled: tuple[str, ...]
    closed: tuple[ClosedPosition, ...]
    timed_out: tuple[str, ...]
    skipped: tuple[str, ...] = ()


def _poll(
    adapter: ExchangeAdapter,
    order: ExchangeOrder,
    config: ExecutionConfig,
    *,
    clock: Callable[[], int],
    sleep: Callable[[float], None],
) -> tuple[ExchangeOrder, bool]:
    deadline = clock() + config.order_timeout_ms
    max_attempts = max(1, config.order_timeout_ms // config.poll_interval_ms)
    latest = order
    attempts = 0
    while not latest.terminal and attempts < max_attempts:
        if clock() >= deadline:
            break
        sleep(config.poll_interval_ms / 1000)
        attempts += 1
        latest = adapter.fetch_order(order.exchange_order_id, order.symbol)
    return latest, not latest.terminal


def flatten(
    risk: RiskService,
    store: ExecutionStore,
    adapter: ExchangeAdapter,
    config: ExecutionConfig,
    confirmation: str,
    *,
    clock: Callable[[], int],
    sleep: Callable[[float], None] = time.sleep,
) -> FlattenResult:
    if confirmation != FLATTEN_PHRASE:
        raise IntentError(f'flatten requires the exact phrase "{FLATTEN_PHRASE}"')

    now = clock()
    reporter = FillReporter(risk, store)
    canceled: list[str] = []
    timed_out: list[str] = []
    skipped: list[str] = []

    for order in adapter.open_orders():
        canceled_order = adapter.cancel_order(order.exchange_order_id, order.symbol)
        canceled.append(order.client_order_id)
        intent = store.get_intent(order.client_order_id)
        if intent is not None:
            reporter.apply(intent, canceled_order, now_ms=now)
        else:
            store.append_event(
                OrderEvent(
                    f"nx-unknown-{order.client_order_id}",
                    EventType.FLATTENED,
                    now,
                    {
                        "action": "cancel",
                        "client_order_id": order.client_order_id,
                        "exchange_order_id": order.exchange_order_id,
                    },
                )
            )

    closed: list[ClosedPosition] = []
    balances = adapter.balances()
    for position in risk.positions(now_ms=now):
        base = position.symbol.split("/")[0]
        available = balances.get(base, Decimal(0))
        quantity = min(position.quantity, available)
        if quantity <= 0:
            skipped.append(position.signal_id)
            store.append_event(
                OrderEvent(
                    intent_id_for(position.signal_id),
                    EventType.FLATTENED,
                    now,
                    {
                        "action": "skip",
                        "reason": "no_exchange_balance",
                        "risk_quantity": str(position.quantity),
                        "exchange_quantity": str(available),
                    },
                )
            )
            continue
        client_order_id = intent_id_for(f"flatten:{position.signal_id}")
        order = adapter.create_market_order(position.symbol, "sell", quantity, client_order_id)
        order, is_open = _poll(adapter, order, config, clock=clock, sleep=sleep)
        if is_open:
            timed_out.append(position.signal_id)

        filled = order.filled_quantity
        if filled > position.quantity:
            raise ExchangeError(
                "flatten sell filled more than the open position", code="overfilled"
            )
        if filled > 0:
            if order.average_price is None:
                raise ExchangeError(
                    "flatten sell reported a fill without an average price",
                    code="bad_fill",
                )
            risk.record_fill(
                position.signal_id,
                Fill(
                    action=FillAction.CLOSE,
                    symbol=position.symbol,
                    quantity=filled,
                    price=order.average_price,
                    fee=order.fee,
                    filled_at_ms=now,
                ),
                now_ms=now,
                fill_key=f"flatten:{client_order_id}:{filled}",
            )
        closed.append(
            ClosedPosition(
                signal_id=position.signal_id,
                symbol=position.symbol,
                requested_quantity=position.quantity,
                filled_quantity=filled,
                average_price=order.average_price,
                fee=order.fee,
            )
        )
        store.append_event(
            OrderEvent(
                intent_id_for(position.signal_id),
                EventType.FLATTENED,
                now,
                {
                    "action": "close",
                    "client_order_id": client_order_id,
                    "exchange_order_id": order.exchange_order_id,
                    "status": str(order.status),
                    "requested_quantity": str(position.quantity),
                    "filled_quantity": str(filled),
                    "average_price": (
                        None if order.average_price is None else str(order.average_price)
                    ),
                    "fee": str(order.fee),
                },
            )
        )

    return FlattenResult(
        canceled=tuple(canceled),
        closed=tuple(closed),
        timed_out=tuple(timed_out),
        skipped=tuple(skipped),
    )
