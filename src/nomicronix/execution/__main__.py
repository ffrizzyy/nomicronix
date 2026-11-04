"""Command line entry point: python -m nomicronix.execution <command>."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from decimal import Decimal

from nomicronix.execution.adapter import (
    build_adapter,
    credentials_from_env,
    masked_key,
)
from nomicronix.execution.config import load_execution_config
from nomicronix.execution.models import ExecutionError, OrderIntent
from nomicronix.execution.service import ExecutionService
from nomicronix.execution.store import ExecutionStore
from nomicronix.risk_engine.config import ConfigError as RiskConfigError
from nomicronix.risk_engine.config import load_limits
from nomicronix.risk_engine.service import RiskService
from nomicronix.risk_engine.store import RiskStateStore


def _add_storage(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--execution-db", default="data/execution.sqlite")
    parser.add_argument("--risk-db", default="data/risk.sqlite")
    parser.add_argument("--risk-config", default=None)
    parser.add_argument("--config", default=None, help="TOML execution config")
    parser.add_argument("--cash", default="1000", help="initial paper cash for a fresh risk DB")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m nomicronix.execution")
    commands = parser.add_subparsers(dest="command", required=True)

    shadow = commands.add_parser("shadow", help="build intents from a signal file")
    _add_storage(shadow)
    shadow.add_argument("--signals", required=True, help="strategy-engine result JSON")

    status = commands.add_parser("status", help="journal and stage summary")
    _add_storage(status)
    status.add_argument("--json", action="store_true", help="machine-readable output")

    show = commands.add_parser("show", help="one intent and its journal")
    _add_storage(show)
    show.add_argument("--intent", required=True)
    show.add_argument("--json", action="store_true", help="machine-readable output")

    submit = commands.add_parser("submit", help="submit one approved intent")
    _add_storage(submit)
    submit.add_argument("--intent", required=True)
    submit.add_argument("--confirm", required=True)
    submit.add_argument("--live", action="store_true", help="required with dry_run = false")
    submit.add_argument("--json", action="store_true", help="machine-readable output")

    cancel = commands.add_parser("cancel", help="cancel one open order")
    _add_storage(cancel)
    cancel.add_argument("--intent", required=True)
    cancel.add_argument("--json", action="store_true", help="machine-readable output")

    reconcile = commands.add_parser("reconcile", help="compare local and exchange books")
    _add_storage(reconcile)
    reconcile.add_argument("--json", action="store_true", help="machine-readable output")

    panic = commands.add_parser("flatten", help="cancel orders and close positions")
    _add_storage(panic)
    panic.add_argument("--confirm", required=True)
    panic.add_argument("--json", action="store_true", help="machine-readable output")

    return parser


@contextmanager
def _stack(args: argparse.Namespace, *, private: bool) -> Iterator[ExecutionService]:
    config = load_execution_config(args.config)
    if private:
        api_key, _ = credentials_from_env()
        print(
            f"exchange key {masked_key(api_key)}: trade-only, withdrawals must be disabled",
            file=sys.stderr,
        )
    adapter = build_adapter(config, require_private=private)
    risk_store = RiskStateStore(args.risk_db)
    store = ExecutionStore(args.execution_db)
    try:
        risk = RiskService(
            risk_store,
            load_limits(args.risk_config),
            initial_cash=Decimal(args.cash),
        )
        yield ExecutionService(risk, store, adapter, config)
    finally:
        store.close()
        risk_store.close()


def _intent_line(intent: OrderIntent) -> str:
    return (
        f"{intent.state}: {intent.intent_id} "
        f"{intent.symbol} qty={intent.quantity} "
        f"filled={intent.filled_quantity} order={intent.exchange_order_id}"
    )


def _run_shadow(args: argparse.Namespace) -> int:
    with _stack(args, private=False) as service:
        intents = service.shadow_file(args.signals)
        for intent in intents:
            print(_intent_line(intent))
        return 0


def _run_status(args: argparse.Namespace) -> int:
    with _stack(args, private=False) as service:
        payload = service.status()
    if args.json:
        print(json.dumps(payload, sort_keys=True, indent=2))
        return 0
    print(f"stage: {payload['stage']} | dry_run: {payload['dry_run']}")
    print(f"intents: {payload['intents']} | open orders: {payload['open_orders']}")
    by_state = payload["by_state"]
    assert isinstance(by_state, dict)
    print(" ".join(f"{state}={count}" for state, count in by_state.items()))
    return 0


def _run_show(args: argparse.Namespace) -> int:
    with _stack(args, private=False) as service:
        intent, events = service.show(args.intent)
        summary = service.summary(intent)
    payload = {
        "intent": summary,
        "events": [
            {
                "type": str(event.event_type),
                "at_ms": event.at_ms,
                "payload": event.payload,
            }
            for event in events
        ],
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True, indent=2))
        return 0
    for key, value in summary.items():
        print(f"{key}: {value}")
    print("events:")
    for event in events:
        print(f"  {event.at_ms} {event.event_type} {json.dumps(event.payload, sort_keys=True)}")
    return 0


def _run_submit(args: argparse.Namespace) -> int:
    with _stack(args, private=True) as service:
        result = service.submit(args.intent, args.confirm, live=args.live)
        summary = service.summary(result.intent)
    payload = {
        "intent": summary,
        "timed_out": result.timed_out,
        "order": None
        if result.order is None
        else {
            "exchange_order_id": result.order.exchange_order_id,
            "status": str(result.order.status),
            "filled_quantity": str(result.order.filled_quantity),
            "average_price": (
                None if result.order.average_price is None else str(result.order.average_price)
            ),
            "fee": str(result.order.fee),
        },
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True, indent=2))
    else:
        print(_intent_line(result.intent))
        if result.timed_out:
            print("warning: order is still open; run reconcile before anything else")
    return 0


def _run_cancel(args: argparse.Namespace) -> int:
    with _stack(args, private=True) as service:
        intent = service.cancel(args.intent)
        summary = service.summary(intent)
    if args.json:
        print(json.dumps(summary, sort_keys=True, indent=2))
    else:
        print(_intent_line(intent))
    return 0


def _run_reconcile(args: argparse.Namespace) -> int:
    with _stack(args, private=True) as service:
        report = service.reconcile()
    payload = {
        "clean": report.clean,
        "matched": list(report.matched),
        "unknown_orders": list(report.unknown_orders),
        "missing_orders": list(report.missing_orders),
        "unknown_positions": list(report.unknown_positions),
        "detail": list(report.detail),
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True, indent=2))
    else:
        print("clean" if report.clean else "not clean")
        for line in report.detail:
            print(f"  {line}")
    return 0 if report.clean else 3


def _run_flatten(args: argparse.Namespace) -> int:
    with _stack(args, private=True) as service:
        result = service.flatten(args.confirm)
    payload = {
        "canceled": list(result.canceled),
        "closed": [
            {
                "signal_id": closed.signal_id,
                "symbol": closed.symbol,
                "requested_quantity": str(closed.requested_quantity),
                "filled_quantity": str(closed.filled_quantity),
                "average_price": (
                    None if closed.average_price is None else str(closed.average_price)
                ),
                "fee": str(closed.fee),
            }
            for closed in result.closed
        ],
        "timed_out": list(result.timed_out),
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True, indent=2))
    else:
        for client_order_id in result.canceled:
            print(f"canceled: {client_order_id}")
        for closed in result.closed:
            print(
                f"closed: {closed.signal_id} {closed.symbol} "
                f"{closed.filled_quantity}/{closed.requested_quantity} "
                f"at {closed.average_price}"
            )
        for signal_id in result.timed_out:
            print(f"still open: {signal_id}")
    incomplete = bool(result.timed_out) or any(
        closed.filled_quantity < closed.requested_quantity for closed in result.closed
    )
    return 3 if incomplete else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "shadow":
            return _run_shadow(args)
        if args.command == "status":
            return _run_status(args)
        if args.command == "show":
            return _run_show(args)
        if args.command == "submit":
            return _run_submit(args)
        if args.command == "cancel":
            return _run_cancel(args)
        if args.command == "reconcile":
            return _run_reconcile(args)
        return _run_flatten(args)
    except ExecutionError as exc:
        print(f"execution error: {exc}", file=sys.stderr)
        return exc.exit_code
    except (
        RiskConfigError,
        ValueError,
        OSError,
        sqlite3.Error,
        ArithmeticError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
