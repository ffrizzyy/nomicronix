"""Command line entry point: python -m nomicronix.risk_engine <command> ..."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from nomicronix.risk_engine.config import ConfigError, load_limits
from nomicronix.risk_engine.models import (
    Instrument,
    RiskDecision,
    RiskStatus,
    Side,
    TradeProposal,
)
from nomicronix.risk_engine.service import RiskService
from nomicronix.risk_engine.store import RiskStateStore, StoreError


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=_json_default))


def _read_json(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _decimal(data: dict[str, Any], key: str) -> Decimal:
    return Decimal(str(data[key]))


def _proposal_from(data: dict[str, Any]) -> tuple[TradeProposal, Instrument]:
    instrument_data = data["instrument"]
    if not isinstance(instrument_data, dict):
        raise ValueError("proposal.instrument must be an object")
    instrument = Instrument(
        symbol=str(instrument_data["symbol"]),
        quantity_step=_decimal(instrument_data, "quantity_step"),
        min_quantity=_decimal(instrument_data, "min_quantity"),
        min_notional=_decimal(instrument_data, "min_notional"),
    )
    proposal = TradeProposal(
        signal_id=str(data["signal_id"]),
        strategy_id=str(data["strategy_id"]),
        symbol=str(data["symbol"]),
        side=Side(str(data["side"])),
        entry_price=_decimal(data, "entry_price"),
        stop_price=_decimal(data, "stop_price"),
        target_price=_decimal(data, "target_price"),
        confidence=float(data["confidence"]),
        created_at_ms=int(data["created_at_ms"]),
    )
    return proposal, instrument


def _prices_from(values: Sequence[str] | None) -> dict[str, Decimal]:
    prices: dict[str, Decimal] = {}
    for value in values or ():
        symbol, separator, price = value.partition("=")
        if not separator or not symbol or not price:
            raise ValueError(f"expected SYMBOL=PRICE, got {value!r}")
        prices[symbol] = Decimal(price)
    return prices


def _decimal_text(value: Decimal) -> str:
    """Plain decimal string without exponent or trailing zeros."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _status_payload(status: RiskStatus) -> dict[str, Any]:
    return {
        "equity": _decimal_text(status.equity),
        "peak_equity": _decimal_text(status.peak_equity),
        "drawdown_pct": _decimal_text(status.drawdown_pct),
        "day_start_equity": _decimal_text(status.day_start_equity),
        "daily_pnl": _decimal_text(status.daily_pnl),
        "daily_loss_pct": _decimal_text(status.daily_loss_pct),
        "open_positions": status.open_positions,
        "open_risk": _decimal_text(status.open_risk),
        "exposure": _decimal_text(status.exposure),
        "trading_allowed": status.trading_allowed,
        "kill_switch": None if status.kill_switch is None else asdict(status.kill_switch),
        "last_mark_ms": status.last_mark_ms,
    }


def _decision_payload(decision: RiskDecision) -> dict[str, Any]:
    return {
        "approved": decision.approved,
        "signal_id": decision.signal_id,
        "approved_quantity": _decimal_text(decision.approved_quantity),
        "approved_notional": _decimal_text(decision.approved_notional),
        "risk_amount": _decimal_text(decision.risk_amount),
        "reward_risk": _decimal_text(decision.reward_risk),
        "reasons": [reason.value for reason in decision.reasons],
        "rule_trace": list(decision.rule_trace),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m nomicronix.risk_engine")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default="data/risk.sqlite")
    common.add_argument("--config", default="config/risk.toml")
    common.add_argument("--cash", default="1000", help="initial paper cash")
    common.add_argument("--now-ms", type=int, default=None, help=argparse.SUPPRESS)

    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", parents=[common], help="show risk status")
    status.set_defaults(handler="status")

    check = commands.add_parser("check", parents=[common], help="evaluate a proposal")
    check.add_argument("--proposal", required=True)
    check.add_argument(
        "--price",
        action="append",
        default=None,
        metavar="SYMBOL=PRICE",
        help="mark this price before checking; repeatable",
    )
    check.set_defaults(handler="check")

    mark = commands.add_parser("mark", parents=[common], help="mark positions to market")
    mark.add_argument("--prices", required=True, help="JSON object of symbol to price")
    mark.set_defaults(handler="mark")

    kill = commands.add_parser("kill", parents=[common], help="engage the kill switch")
    kill.add_argument("--reason", required=True)
    kill.set_defaults(handler="kill")

    resume = commands.add_parser("resume", parents=[common], help="clear the kill switch")
    resume.add_argument("--confirm", required=True)
    resume.set_defaults(handler="resume")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        limits = load_limits(args.config)
        store = RiskStateStore(args.db)
        service = RiskService(store, limits, initial_cash=Decimal(args.cash))
    except (ConfigError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        now = args.now_ms
        if args.handler == "status":
            _print(_status_payload(service.status(now)))
            return 0
        if args.handler == "mark":
            prices = {
                str(symbol): Decimal(str(price))
                for symbol, price in _read_json(args.prices).items()
            }
            _print(_status_payload(service.mark(prices, now)))
            return 0
        if args.handler == "check":
            prices = _prices_from(args.price)
            if prices:
                service.mark(prices, now)
            proposal, instrument = _proposal_from(_read_json(args.proposal))
            decision = service.check(proposal, instrument, now)
            _print(_decision_payload(decision))
            return 0 if decision.approved else 2
        if args.handler == "kill":
            service.engage_kill_switch(args.reason, now_ms=now)
            _print(_status_payload(service.status(now)))
            return 0
        if args.handler == "resume":
            service.resume(args.confirm, now)
            _print(_status_payload(service.status(now)))
            return 0
        raise ValueError(f"unknown command {args.handler}")
    except (KeyError, OSError, StoreError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
