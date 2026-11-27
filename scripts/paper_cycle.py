"""Run one paper-trading cycle: backfill, mark, scan, deliver, readiness.

This is operational glue, not a new module. It calls the same CLIs documented in
the README, in a causal order:

1. `market-data backfill` refreshes stored candles.
2. `signal-delivery tick` marks open paper trades at the latest closed candle.
3. `strategy-engine scan` proposes signals from the new candles.
4. `signal-delivery deliver` risk-checks them, alerts and opens paper trades.
5. `signal-delivery readiness` reports the promotion gate.

Marking before delivery means a trade opened this cycle is never stopped or
targeted on the same candle that created it.

Scheduling is the owner's job. One run does one cycle; a scheduler (cron, Task
Scheduler, a timer unit) decides how often. A readiness failure is reported but
does not fail the cycle, because the gate is expected to stay red while the
record accumulates.

Usage, from the repository root:

    uv run python scripts/paper_cycle.py
    uv run python scripts/paper_cycle.py --dry-run
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nomicronix.market_data.models import Timeframe
from nomicronix.market_data.store import SqliteStore

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class Step:
    """One CLI invocation, without the Python executable."""

    name: str
    args: tuple[str, ...]

    def command(self) -> str:
        return " ".join(shlex.quote(part) for part in ("python", *self.args))


@dataclass(frozen=True, slots=True)
class CyclePlan:
    symbols: tuple[str, ...]
    exchange: str
    timeframe: str
    since: str
    until_ms: int
    strategy: str
    params: tuple[str, ...]
    min_confidence: str
    ttl: str
    market_db: str
    delivery_db: str
    risk_db: str
    results_dir: str
    jsonl: str
    cash: str
    sinks: tuple[str, ...]
    prices: tuple[tuple[str, str], ...] = ()
    api_url: str | None = None


def _slug(symbol: str) -> str:
    return symbol.replace("/", "").replace(":", "").replace("-", "")


def build_steps(plan: CyclePlan) -> list[Step]:
    """Build the cycle in order. Pure, so the plan is testable without a network."""
    steps = [
        Step(
            name="backfill",
            args=(
                "-m",
                "nomicronix.market_data",
                "backfill",
                "--symbols",
                ",".join(plan.symbols),
                "--timeframes",
                plan.timeframe,
                "--exchange",
                plan.exchange,
                "--since",
                plan.since,
                "--db",
                plan.market_db,
                *(("--api-url", plan.api_url) if plan.api_url else ()),
            ),
        )
    ]

    if plan.prices:
        steps.append(
            Step(
                name="tick",
                args=(
                    "-m",
                    "nomicronix.signal_delivery",
                    "tick",
                    "--delivery-db",
                    plan.delivery_db,
                    "--risk-db",
                    plan.risk_db,
                    "--cash",
                    plan.cash,
                    *(
                        argument
                        for symbol, price in plan.prices
                        for argument in ("--price", f"{symbol}={price}")
                    ),
                ),
            )
        )

    signal_files: list[tuple[str, str]] = []
    for symbol in plan.symbols:
        destination = str(Path(plan.results_dir) / f"paper-{_slug(symbol)}-signals.json")
        signal_files.append((symbol, destination))
        steps.append(
            Step(
                name=f"scan {symbol}",
                args=(
                    "-m",
                    "nomicronix.strategy_engine",
                    "scan",
                    "--db",
                    plan.market_db,
                    "--exchange",
                    plan.exchange,
                    "--symbol",
                    symbol,
                    "--timeframe",
                    plan.timeframe,
                    "--since",
                    plan.since,
                    "--until",
                    str(plan.until_ms),
                    "--strategy",
                    plan.strategy,
                    *(argument for param in plan.params for argument in ("--param", param)),
                    "--min-confidence",
                    plan.min_confidence,
                    "--ttl",
                    plan.ttl,
                    "--out",
                    destination,
                    "--force",
                ),
            )
        )

    for symbol, destination in signal_files:
        steps.append(
            Step(
                name=f"deliver {symbol}",
                args=(
                    "-m",
                    "nomicronix.signal_delivery",
                    "deliver",
                    "--signals",
                    destination,
                    "--delivery-db",
                    plan.delivery_db,
                    "--risk-db",
                    plan.risk_db,
                    "--cash",
                    plan.cash,
                    *(argument for sink in plan.sinks for argument in ("--sink", sink)),
                    "--jsonl",
                    plan.jsonl,
                ),
            )
        )

    steps.append(
        Step(
            name="readiness",
            args=(
                "-m",
                "nomicronix.signal_delivery",
                "readiness",
                "--delivery-db",
                plan.delivery_db,
                "--risk-db",
                plan.risk_db,
                "--cash",
                plan.cash,
            ),
        )
    )
    return steps


def execute(steps: Sequence[Step], runner: Callable[[Step], int]) -> int:
    """Run every step; stop on a failure. Readiness may report not-ready (2)."""
    for step in steps:
        code = runner(step)
        if step.name == "readiness" and code in (0, 2):
            if code == 2:
                print("readiness: still accumulating paper evidence (exit 2)")
            continue
        if code != 0:
            print(f"stopped: {step.name} failed with exit {code}")
            return code
    return 0


def latest_prices(
    market_db: str,
    exchange: str,
    symbols: Sequence[str],
    timeframe: str,
) -> tuple[tuple[str, str], ...]:
    """Latest closed candle close per symbol, for the tick mark."""
    if not Path(market_db).exists():
        return ()
    store = SqliteStore(market_db, exchange)
    try:
        prices: list[tuple[str, str]] = []
        for symbol in symbols:
            candle = store.latest_candle(symbol, timeframe)
            if candle is not None:
                prices.append((symbol, str(candle.close)))
        return tuple(prices)
    finally:
        store.close()


def _manual_prices(values: Sequence[str]) -> tuple[tuple[str, str], ...]:
    prices: list[tuple[str, str]] = []
    for value in values:
        symbol, separator, price = value.partition("=")
        if not separator or not symbol or not price:
            raise ValueError(f"expected SYMBOL=PRICE, got {value!r}")
        prices.append((symbol, price))
    return tuple(prices)


def _runner(*, dry_run: bool, timeout: float) -> Callable[[Step], int]:
    def run(step: Step) -> int:
        print(f"==> {step.name}")
        print(f"    {step.command()}", flush=True)
        if dry_run:
            return 0
        try:
            completed = subprocess.run(
                [sys.executable, *step.args],
                cwd=ROOT,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            print(f"error: {step.name} exceeded {timeout:.0f}s")
            return 1
        return completed.returncode

    return run


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/paper_cycle.py",
        description="Run one paper-trading cycle and report readiness.",
    )
    parser.add_argument("--symbols", default="BTC/USDT,ETH/USDT")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--timeframe", default="1h", choices=[item.value for item in Timeframe])
    parser.add_argument("--since", default=None, help="UTC date; default: before --lookback-days")
    parser.add_argument("--lookback-days", type=int, default=120)
    parser.add_argument("--strategy", default="examples.strategy_models:SmaCross")
    parser.add_argument(
        "--param",
        action="append",
        default=None,
        help="strategy parameter KEY=VALUE, repeatable; default fast=20 slow=50",
    )
    parser.add_argument("--min-confidence", default="0.5")
    parser.add_argument("--ttl", default="5m")
    parser.add_argument(
        "--api-url",
        default=None,
        help=(
            "market-data spot base URL (binance only), e.g. https://data-api.binance.vision/api/v3"
        ),
    )
    parser.add_argument("--market-db", default="data/market.sqlite")
    parser.add_argument("--delivery-db", default="data/delivery.sqlite")
    parser.add_argument("--risk-db", default="data/risk.sqlite")
    parser.add_argument("--results-dir", default="results")
    parser.add_argument("--jsonl", default="data/deliveries.jsonl")
    parser.add_argument("--cash", default="1000")
    parser.add_argument(
        "--sink",
        action="append",
        default=None,
        choices=["console", "jsonl", "telegram"],
        help="repeatable; default console and jsonl",
    )
    parser.add_argument(
        "--price",
        action="append",
        default=None,
        metavar="SYMBOL=PRICE",
        help="override the tick mark; repeatable; default: latest stored candle close",
    )
    parser.add_argument("--timeout", type=float, default=1800.0, help="seconds per step")
    parser.add_argument("--dry-run", action="store_true", help="print the commands only")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        timeframe = Timeframe(args.timeframe)
        now = datetime.now(UTC)
        until_ms = int(now.timestamp() * 1000) // timeframe.ms * timeframe.ms
        since = args.since or (now.date() - timedelta(days=args.lookback_days)).isoformat()
        symbols = tuple(item.strip() for item in args.symbols.split(",") if item.strip())
        if not symbols:
            raise ValueError("at least one symbol is required")
        params = tuple(args.param) if args.param else ("fast=20", "slow=50")
        prices = (
            _manual_prices(args.price)
            if args.price
            else latest_prices(args.market_db, args.exchange, symbols, timeframe.value)
        )
        plan = CyclePlan(
            symbols=symbols,
            exchange=args.exchange,
            timeframe=timeframe.value,
            since=since,
            until_ms=until_ms,
            strategy=args.strategy,
            params=params,
            min_confidence=args.min_confidence,
            ttl=args.ttl,
            market_db=args.market_db,
            delivery_db=args.delivery_db,
            risk_db=args.risk_db,
            results_dir=args.results_dir,
            jsonl=args.jsonl,
            cash=args.cash,
            sinks=tuple(args.sink) if args.sink else ("console", "jsonl"),
            prices=prices,
            api_url=args.api_url,
        )
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not prices:
        print("note: no stored prices yet; the tick step runs from the next cycle")
    return execute(build_steps(plan), _runner(dry_run=args.dry_run, timeout=args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
