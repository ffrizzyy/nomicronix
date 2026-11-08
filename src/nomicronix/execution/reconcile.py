"""Compare the local journal with the exchange and the risk book.

Reconciliation is mandatory before a live submission. It is deliberately
strict: an order or balance that cannot be explained by the journal blocks new
orders until the owner resolves it. There is no "ignore" switch.
"""

from __future__ import annotations

from decimal import Decimal

from nomicronix.execution.adapter import ExchangeAdapter
from nomicronix.execution.models import (
    EventType,
    ExchangeError,
    IntentState,
    OrderEvent,
    Reconciliation,
)
from nomicronix.execution.store import ExecutionStore
from nomicronix.risk_engine.service import RiskService

_QUOTE_ASSETS = frozenset({"USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI"})
_TOLERANCE = Decimal("0.00000001")


def _quoted_symbol(asset: str) -> str:
    return f"{asset}/USDT"


def reconcile(
    risk: RiskService,
    store: ExecutionStore,
    adapter: ExchangeAdapter,
    now_ms: int,
    *,
    journal_mismatches: bool = True,
) -> Reconciliation:
    matched: list[str] = []
    missing: list[str] = []
    unknown: list[str] = []
    unknown_positions: list[str] = []
    detail: list[str] = []

    open_orders = adapter.open_orders()
    open_by_client = {order.client_order_id: order for order in open_orders}

    submitted = store.list_intents([IntentState.SUBMITTED])
    for intent in submitted:
        if intent.exchange_order_id is None:
            matched_open = open_by_client.get(intent.intent_id)
            if matched_open is None:
                missing.append(intent.intent_id)
                detail.append(f"missing_order: {intent.intent_id} has no exchange order id")
                continue
            store.update_intent(
                intent.intent_id,
                exchange_order_id=matched_open.exchange_order_id,
                event=OrderEvent(
                    intent.intent_id,
                    EventType.RECONCILED,
                    now_ms,
                    {"attached_exchange_order_id": matched_open.exchange_order_id},
                ),
            )
            matched.append(intent.intent_id)
            continue
        try:
            adapter.fetch_order(intent.exchange_order_id, intent.symbol)
        except ExchangeError as exc:
            if exc.code != "unknown_order":
                raise
            missing.append(intent.intent_id)
            detail.append(f"missing_order: {intent.intent_id} is not at the exchange")
        else:
            matched.append(intent.intent_id)

    submitted_ids = {intent.intent_id for intent in submitted}
    for order in open_orders:
        if order.client_order_id not in submitted_ids:
            unknown.append(order.client_order_id)
            detail.append(f"unknown_order: {order.client_order_id} ({order.exchange_order_id})")

    balances = adapter.balances()
    positions = risk.positions(now_ms=now_ms)
    position_bases = {position.symbol.split("/")[0] for position in positions}
    for position in positions:
        base = position.symbol.split("/")[0]
        held = balances.get(base, Decimal(0))
        if held + _TOLERANCE < position.quantity:
            unknown_positions.append(position.symbol)
            detail.append(
                f"unknown_position: {position.symbol} risk holds {position.quantity} "
                f"but the exchange holds {held}"
            )
    for asset, amount in sorted(balances.items()):
        if asset in _QUOTE_ASSETS or amount <= _TOLERANCE:
            continue
        if asset not in position_bases:
            unknown_positions.append(_quoted_symbol(asset))
            detail.append(
                f"unknown_position: exchange holds {amount} {asset} with no risk position"
            )

    report = Reconciliation(
        matched=tuple(matched),
        unknown_orders=tuple(unknown),
        missing_orders=tuple(missing),
        unknown_positions=tuple(unknown_positions),
        detail=tuple(detail),
    )

    if journal_mismatches and not report.clean:
        _journal(store, report, now_ms)

    return report


def _journal(store: ExecutionStore, report: Reconciliation, now_ms: int) -> None:
    payload: dict[str, object] = {
        "clean": report.clean,
        "matched": list(report.matched),
        "unknown_orders": list(report.unknown_orders),
        "missing_orders": list(report.missing_orders),
        "unknown_positions": list(report.unknown_positions),
        "detail": list(report.detail),
    }
    targets = list(report.missing_orders) + list(report.unknown_orders) or ["nx-reconcile"]
    for target in targets:
        intent_id = target if target.startswith("nx-") else f"nx-unknown-{target}"
        store.append_event(OrderEvent(intent_id, EventType.RECONCILED, now_ms, payload))
