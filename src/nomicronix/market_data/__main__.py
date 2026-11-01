"""Command line entry point: python -m nomicronix.market_data <command> ..."""

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from nomicronix.market_data.adapter import CcxtAdapter, ExchangeAdapter
from nomicronix.market_data.backfill import BackfillConfig, run_backfill
from nomicronix.market_data.models import Timeframe
from nomicronix.market_data.store import SqliteStore


def parse_since(value: str) -> int:
    """'2025-01-01' -> UTC epoch milliseconds. Raises ValueError for anything else."""
    parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _timeframes(value: str) -> list[str]:
    return [Timeframe(item).value for item in _csv(value)]  # ValueError if unsupported


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m nomicronix.market_data")
    commands = parser.add_subparsers(dest="command", required=True)

    backfill = commands.add_parser("backfill", help="download historical candles")
    backfill.add_argument("--exchange", default="binance")
    backfill.add_argument("--symbols", type=_csv, required=True, help="e.g. BTC/USDT,ETH/USDT")
    backfill.add_argument("--timeframes", type=_timeframes, required=True, help="e.g. 1h,15m")
    backfill.add_argument("--since", type=parse_since, required=True, help="UTC date, YYYY-MM-DD")
    backfill.add_argument("--db", default="data/market.sqlite")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    adapter: ExchangeAdapter | None = None,
    config: BackfillConfig | None = None,
) -> int:
    """Run a command. `adapter` and `config` are injectable for tests."""
    args = _build_parser().parse_args(argv)
    adapter = adapter or CcxtAdapter(args.exchange)
    config = config or BackfillConfig()
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
