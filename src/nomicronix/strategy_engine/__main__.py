"""Command line entry point: python -m nomicronix.strategy_engine <command> ..."""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

from nomicronix.market_data.store import SqliteStore
from nomicronix.strategy_engine.config import ConfigError, load_scan_defaults
from nomicronix.strategy_engine.data import DataError
from nomicronix.strategy_engine.engine import SignalLimitError, scan
from nomicronix.strategy_engine.models import ScanConfig
from nomicronix.strategy_engine.proposers import SignalProposer
from nomicronix.strategy_engine.results import summary, write_result


def parse_date(value: str) -> int:
    parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def parse_time(value: str) -> int:
    """Accept a UTC date or an epoch-millisecond integer."""
    if value.isdigit():
        return int(value)
    return parse_date(value)


def parse_duration(value: str) -> int:
    units = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
    if len(value) < 2 or value[-1] not in units or not value[:-1].isdigit():
        raise ValueError(f"expected duration like 30s, 5m, 12h or 1d, got {value!r}")
    return int(value[:-1]) * units[value[-1]]


def _parse_value(value: str) -> object:
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _parse_params(values: Sequence[str] | None) -> dict[str, object]:
    params: dict[str, object] = {}
    for value in values or ():
        key, separator, raw = value.partition("=")
        if not separator or not key:
            raise ValueError(f"expected KEY=VALUE, got {value!r}")
        params[key] = _parse_value(raw)
    return params


def _build_proposer(spec: str, params: Mapping[str, object]) -> SignalProposer:
    module_name, separator, class_name = spec.partition(":")
    if not separator or not module_name or not class_name:
        raise ValueError(f"expected module:Class, got {spec!r}")
    module = importlib.import_module(module_name)
    candidate = getattr(module, class_name)
    if not isinstance(candidate, type):
        raise TypeError(f"{spec} is not a class")
    instance = candidate(**params)
    strategy_id = getattr(instance, "strategy_id", None)
    min_history = getattr(instance, "min_history", None)
    if not isinstance(strategy_id, str) or not strategy_id:
        raise TypeError(f"{spec} must expose a non-empty strategy_id")
    if not isinstance(min_history, int) or min_history < 1:
        raise TypeError(f"{spec} must expose a positive integer min_history")
    if not callable(getattr(instance, "propose", None)):
        raise TypeError(f"{spec} must expose propose(view)")
    return cast(SignalProposer, instance)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default="data/market.sqlite")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True)
    parser.add_argument("--since", required=True, help="UTC date or epoch milliseconds")
    parser.add_argument("--until", required=True, help="UTC date or epoch milliseconds, exclusive")
    parser.add_argument("--config", default=None, help="TOML scan defaults")
    parser.add_argument("--min-history", type=int, default=None)
    parser.add_argument("--min-confidence", default=None)
    parser.add_argument("--ttl", default=None, help="signal lifetime, e.g. 5m")
    parser.add_argument("--max-signals", type=int, default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--force", action="store_true")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m nomicronix.strategy_engine")
    commands = parser.add_subparsers(dest="command", required=True)

    scan_command = commands.add_parser("scan", help="scan stored candles for signals")
    _add_common(scan_command)
    scan_command.add_argument("--strategy", required=True, help="module:Class")
    scan_command.add_argument(
        "--param", action="append", default=None, help="KEY=VALUE, repeatable"
    )
    scan_command.set_defaults(handler="scan")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    store: SqliteStore | None = None
    try:
        defaults = load_scan_defaults(args.config)
        params = _parse_params(args.param)

        config = ScanConfig(
            symbol=args.symbol,
            timeframe=args.timeframe,
            start_ms=parse_time(args.since),
            end_ms=parse_time(args.until),
            min_history=defaults.min_history if args.min_history is None else args.min_history,
            min_confidence=(
                defaults.min_confidence
                if args.min_confidence is None
                else float(args.min_confidence)
            ),
            ttl_ms=defaults.ttl_ms if args.ttl is None else parse_duration(args.ttl),
            max_signals=defaults.max_signals if args.max_signals is None else args.max_signals,
        )
    except (ConfigError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        proposer = _build_proposer(args.strategy, params)
    except (AttributeError, ImportError, TypeError, ValueError) as exc:
        print(f"strategy error: {exc}", file=sys.stderr)
        return 2

    try:
        store = SqliteStore(args.db, args.exchange)
        batch = scan(store, proposer, config)
        print(summary(batch))
        if args.out:
            destination = write_result(batch, args.out, force=args.force)
            print(f"wrote {destination}")
        return 0
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except (DataError, SignalLimitError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
