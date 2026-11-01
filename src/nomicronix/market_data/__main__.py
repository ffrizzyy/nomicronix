"""Command line entry point: python -m nomicronix.market_data <command> ..."""

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from itertools import product

from nomicronix.market_data.adapter import CcxtAdapter, ExchangeAdapter
from nomicronix.market_data.backfill import BackfillConfig, run_backfill
from nomicronix.market_data.live import LiveConfig, run_live
from nomicronix.market_data.models import Timeframe
from nomicronix.market_data.store import SqliteStore
from nomicronix.market_data.verify import format_report, verify_series


def parse_since(value: str) -> int:
    """'2025-01-01' -> UTC epoch milliseconds. Raises ValueError for anything else."""
    parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _timeframes(value: str) -> list[str]:
    return [Timeframe(item).value for item in _csv(value)]  # ValueError if unsupported


def _add_series_args(command: argparse.ArgumentParser) -> None:
    command.add_argument("--exchange", default="binance")
    command.add_argument("--symbols", type=_csv, required=True, help="e.g. BTC/USDT,ETH/USDT")
    command.add_argument("--timeframes", type=_timeframes, required=True, help="e.g. 1h,15m")
    command.add_argument("--db", default="data/market.sqlite")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m nomicronix.market_data")
    commands = parser.add_subparsers(dest="command", required=True)

    backfill = commands.add_parser("backfill", help="download historical candles")
    _add_series_args(backfill)
    backfill.add_argument("--since", type=parse_since, required=True, help="UTC date, YYYY-MM-DD")

    live = commands.add_parser("live", help="keep candles current (runs until Ctrl-C)")
    _add_series_args(live)

    verify = commands.add_parser("verify", help="report gaps in stored candles")
    _add_series_args(verify)
    verify.add_argument("--since", type=parse_since, help="also check from this UTC date")
    verify.add_argument(
        "--refill", action="store_true", help="try to fill gaps from the exchange"
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    adapter: ExchangeAdapter | None = None,
    config: BackfillConfig | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """Run a command and return the process exit code.

    `adapter`, `config` and `should_stop` exist so tests can inject fakes.
    """
    args = _build_parser().parse_args(argv)
    config = config or BackfillConfig()
    if args.command == "backfill":
        return _backfill(args, adapter or CcxtAdapter(args.exchange), config)
    if args.command == "live":
        return _live(args, adapter or CcxtAdapter(args.exchange), config, should_stop)
    return _verify(args, adapter, config)


def _backfill(args: argparse.Namespace, adapter: ExchangeAdapter, config: BackfillConfig) -> int:
    store = SqliteStore(args.db, adapter.name)
    try:
        summary = run_backfill(
            adapter, store, args.symbols, args.timeframes, args.since, config=config
        )
    finally:
        store.close()
    for (symbol, timeframe), count in summary.stored.items():
        print(f"{symbol} {timeframe}: {count} candles written")
    for failure in summary.failures:
        print(f"{failure.symbol} {failure.timeframe}: FAILED: {failure.error}", file=sys.stderr)
    return 1 if summary.failures else 0


def _live(
    args: argparse.Namespace,
    adapter: ExchangeAdapter,
    config: BackfillConfig,
    should_stop: Callable[[], bool] | None,
) -> int:
    store = SqliteStore(args.db, adapter.name)
    try:
        run_live(
            adapter, store, args.symbols, args.timeframes,
            config=LiveConfig(backfill=config),
            should_stop=should_stop or (lambda: False),
        )
    except KeyboardInterrupt:
        print("stopped")
    finally:
        store.close()
    return 0


def _verify(
    args: argparse.Namespace, adapter: ExchangeAdapter | None, config: BackfillConfig
) -> int:
    refill_adapter = (adapter or CcxtAdapter(args.exchange)) if args.refill else None
    store = SqliteStore(args.db, adapter.name if adapter else args.exchange)
    all_ok = True
    try:
        for symbol, timeframe in product(args.symbols, args.timeframes):
            report = verify_series(
                store, symbol, timeframe,
                since_ms=args.since, adapter=refill_adapter, config=config,
            )
            print(format_report(report))
            all_ok = all_ok and report.ok
    finally:
        store.close()
    return 0 if all_ok else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
