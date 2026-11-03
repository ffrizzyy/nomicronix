"""Command line entry point: python -m nomicronix.signal_delivery <command>."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from nomicronix.risk_engine.config import ConfigError as RiskConfigError
from nomicronix.risk_engine.config import load_limits
from nomicronix.risk_engine.models import utc_now_ms
from nomicronix.risk_engine.service import RiskService
from nomicronix.risk_engine.store import RiskStateStore
from nomicronix.signal_delivery.config import (
    DeliveryDefaults,
    load_delivery_defaults,
    load_promotion_criteria,
)
from nomicronix.signal_delivery.models import (
    ConfigError,
    DeliveryAction,
    DeliveryError,
    DeliveryOutcome,
    InstrumentRules,
    SignalFileError,
    StoreError,
)
from nomicronix.signal_delivery.readiness import evaluate_readiness, render_readiness
from nomicronix.signal_delivery.render import notification_from_payload
from nomicronix.signal_delivery.report import build_report, render_report
from nomicronix.signal_delivery.service import DeliveryService
from nomicronix.signal_delivery.sinks import (
    ConsoleSink,
    JsonlSink,
    Sink,
    TelegramSink,
)
from nomicronix.signal_delivery.store import DeliveryStore


def _add_storage(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--delivery-db", default="data/delivery.sqlite")
    parser.add_argument("--risk-db", default="data/risk.sqlite")
    parser.add_argument("--risk-config", default=None)
    parser.add_argument("--cash", default="1000")
    parser.add_argument("--config", default=None, help="TOML delivery defaults")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m nomicronix.signal_delivery")
    commands = parser.add_subparsers(dest="command", required=True)

    deliver = commands.add_parser("deliver", help="risk-check and deliver a signal file")
    _add_storage(deliver)
    deliver.add_argument("--signals", required=True, help="strategy-engine result JSON")
    deliver.add_argument(
        "--sink",
        action="append",
        choices=["console", "jsonl", "telegram"],
        default=None,
        help="repeatable; overrides the config list",
    )
    deliver.add_argument("--jsonl", default=None, help="JSONL output path")
    deliver.add_argument("--quantity-step", default=None)
    deliver.add_argument("--min-quantity", default=None)
    deliver.add_argument("--min-notional", default=None)

    tick = commands.add_parser("tick", help="mark open paper trades")
    _add_storage(tick)
    tick.add_argument("--prices", default=None, help="JSON object of symbol to price")
    tick.add_argument(
        "--price",
        action="append",
        default=None,
        metavar="SYMBOL=PRICE",
        help="repeatable price mark",
    )
    tick.add_argument("--at", default=None, help="UTC epoch milliseconds; default now")

    report = commands.add_parser("report", help="delivery and paper P&L report")
    _add_storage(report)

    readiness = commands.add_parser("readiness", help="check the execution promotion gate")
    _add_storage(readiness)
    readiness.add_argument("--json", action="store_true", help="emit a machine-readable report")
    return parser


def _build_sinks(names: Sequence[str], defaults: DeliveryDefaults, jsonl: str | None) -> list[Sink]:
    if not names:
        raise ConfigError("at least one sink is required")
    sinks: list[Sink] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        if name == "console":
            sinks.append(ConsoleSink())
        elif name == "jsonl":
            sinks.append(JsonlSink(jsonl or defaults.jsonl_path))
        elif name == "telegram":
            sinks.append(TelegramSink.from_env())
        else:
            raise ConfigError(f"unknown sink: {name}")
    return sinks


def _price_map(path: str | None, values: Sequence[str] | None) -> dict[str, Decimal]:
    prices: dict[str, Decimal] = {}
    if path:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"cannot read prices file: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid prices JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("prices file must contain a JSON object")
        for symbol, value in raw.items():
            try:
                prices[str(symbol)] = Decimal(str(value))
            except ArithmeticError as exc:
                raise ValueError(
                    f"price for {symbol!r} is not a decimal number: {value!r}"
                ) from exc
    for value in values or ():
        symbol, separator, raw_price = value.partition("=")
        if not separator or not symbol:
            raise ValueError(f"expected SYMBOL=PRICE, got {value!r}")
        try:
            prices[symbol] = Decimal(raw_price)
        except ArithmeticError as exc:
            raise ValueError(f"price for {symbol!r} is not a decimal number: {value!r}") from exc
    if not prices:
        raise ValueError("at least one price is required")
    for symbol, price in prices.items():
        if not symbol.strip() or price <= 0:
            raise ValueError(f"price for {symbol!r} must be positive")
    return prices


def _decimal_flag(value: str | None, default: Decimal) -> Decimal:
    return default if value is None else Decimal(value)


def _outcome_line(outcome: DeliveryOutcome) -> str:
    line = f"{outcome.action.value}: {outcome.record.signal_id}"
    if outcome.record.status.value == "rejected":
        try:
            payload = json.loads(outcome.record.payload_json)
            notification = notification_from_payload(payload)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return line
        return f"{line} ({', '.join(notification.reasons)})"
    if outcome.record.error:
        return f"{line} ({outcome.record.error})"
    return line


def _run_deliver(args: argparse.Namespace) -> int:
    defaults = load_delivery_defaults(args.config)
    names = tuple(args.sink) if args.sink else defaults.sinks
    sinks = _build_sinks(names, defaults, args.jsonl)
    rules = InstrumentRules(
        quantity_step=_decimal_flag(args.quantity_step, defaults.quantity_step),
        min_quantity=_decimal_flag(args.min_quantity, defaults.min_quantity),
        min_notional=_decimal_flag(args.min_notional, defaults.min_notional),
    )
    risk_store = RiskStateStore(args.risk_db)
    delivery_store = DeliveryStore(args.delivery_db)
    try:
        risk = RiskService(
            risk_store,
            load_limits(args.risk_config),
            initial_cash=Decimal(args.cash),
        )
        service = DeliveryService(risk, delivery_store, sinks, rules)
        outcomes = service.deliver_file(args.signals)
        for outcome in outcomes:
            print(_outcome_line(outcome))
        totals = delivery_store.delivery_totals()
        actions = {action: 0 for action in DeliveryAction}
        for outcome in outcomes:
            actions[outcome.action] += 1
        print(
            f"delivered {actions[DeliveryAction.DELIVERED]} | "
            f"rejected {actions[DeliveryAction.REJECTED]} | "
            f"skipped {actions[DeliveryAction.SKIPPED]} | "
            f"retried {actions[DeliveryAction.RETRIED]}"
        )
        print(
            f"outbox: {totals.total} total | delivered {totals.delivered} | "
            f"rejected {totals.rejected} | pending {totals.pending} | failed {totals.failed}"
        )
        return 0
    finally:
        delivery_store.close()
        risk_store.close()


def _run_tick(args: argparse.Namespace) -> int:
    prices = _price_map(args.prices, args.price)
    now_ms = None if args.at is None else int(args.at)
    risk_store = RiskStateStore(args.risk_db)
    delivery_store = DeliveryStore(args.delivery_db)
    try:
        risk = RiskService(
            risk_store,
            load_limits(args.risk_config),
            initial_cash=Decimal(args.cash),
            clock=utc_now_ms,
        )
        service = DeliveryService(risk, delivery_store, [ConsoleSink()], InstrumentRules())
        result = service.tick(prices, now_ms=now_ms)
        for trade in result.closed:
            print(
                f"closed {trade.signal_id} {trade.exit_reason} "
                f"at {trade.exit_price} pnl {trade.realized_pnl}"
            )
        print(f"closed: {len(result.closed)} | open: {len(result.still_open)}")
        return 0
    finally:
        delivery_store.close()
        risk_store.close()


def _run_report(args: argparse.Namespace) -> int:
    risk_store = RiskStateStore(args.risk_db)
    delivery_store = DeliveryStore(args.delivery_db)
    try:
        risk = RiskService(
            risk_store,
            load_limits(args.risk_config),
            initial_cash=Decimal(args.cash),
        )
        print(render_report(build_report(delivery_store, risk)))
        return 0
    finally:
        delivery_store.close()
        risk_store.close()


def _run_readiness(args: argparse.Namespace) -> int:
    criteria = load_promotion_criteria(args.config)
    delivery_store = DeliveryStore(args.delivery_db)
    try:
        report = evaluate_readiness(
            delivery_store,
            criteria,
            initial_cash=Decimal(args.cash),
        )
        if args.json:
            print(json.dumps(report.payload(), sort_keys=True, indent=2))
        else:
            print(render_readiness(report))
        return 0 if report.ready else 2
    finally:
        delivery_store.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "deliver":
            return _run_deliver(args)
        if args.command == "tick":
            return _run_tick(args)
        if args.command == "readiness":
            return _run_readiness(args)
        return _run_report(args)
    except DeliveryError as exc:
        print(f"delivery error: {exc}", file=sys.stderr)
        return 2
    except (
        ConfigError,
        RiskConfigError,
        SignalFileError,
        StoreError,
        ValueError,
        OSError,
        sqlite3.Error,
        ArithmeticError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
