"""Command line entry point: python -m nomicronix.backtesting <command> ..."""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from nomicronix.backtesting.data import DataError
from nomicronix.backtesting.engine import run_backtest
from nomicronix.backtesting.models import (
    BacktestConfig,
    BacktestResult,
    WalkForwardConfig,
    WalkForwardResult,
)
from nomicronix.backtesting.results import summary, write_result
from nomicronix.backtesting.strategy import Strategy, StrategyFactory
from nomicronix.backtesting.walk_forward import WalkForwardError, run_walk_forward
from nomicronix.market_data.store import SqliteStore


def parse_date(value: str) -> int:
    parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def parse_duration(value: str) -> int:
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    if len(value) < 2 or value[-1] not in units or not value[:-1].isdigit():
        raise ValueError(f"expected duration like 30m, 12h or 90d, got {value!r}")
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


def _parse_grid(values: Sequence[str] | None) -> dict[str, tuple[object, ...]]:
    grid: dict[str, tuple[object, ...]] = {}
    for value in values or ():
        key, separator, raw = value.partition("=")
        if not separator or not key or not raw:
            raise ValueError(f"expected KEY=VALUE1,VALUE2, got {value!r}")
        if key in grid:
            raise ValueError(f"duplicate grid key: {key}")
        grid[key] = tuple(_parse_value(item) for item in raw.split(",") if item)
    return grid


def _load_strategy(spec: str) -> type[Strategy]:
    module_name, separator, class_name = spec.partition(":")
    if not separator or not module_name or not class_name:
        raise ValueError(f"expected module:Class, got {spec!r}")
    module = importlib.import_module(module_name)
    candidate = getattr(module, class_name)
    if not isinstance(candidate, type):
        raise TypeError(f"{spec} is not a class")
    return candidate


def _factory(strategy_class: type[Strategy]) -> StrategyFactory:
    def build(params: Mapping[str, object]) -> Strategy:
        return strategy_class(**params)

    return build


def _config(args: argparse.Namespace) -> BacktestConfig:
    return BacktestConfig(
        symbol=args.symbol,
        timeframe=args.timeframe,
        start_ms=parse_date(args.since),
        end_ms=parse_date(args.until),
        initial_cash=Decimal(args.cash),
        fee_bps=Decimal(args.fee_bps),
        slippage_bps=Decimal(args.slippage_bps),
        position_fraction=Decimal(args.position_fraction),
        min_notional=Decimal(args.min_notional),
        quantity_step=Decimal(args.quantity_step),
        min_candles=args.min_candles,
    )


def _walk_forward_config(args: argparse.Namespace) -> WalkForwardConfig:
    return WalkForwardConfig(
        train_ms=parse_duration(args.train),
        test_ms=parse_duration(args.test),
        step_ms=parse_duration(args.step),
        objective=args.objective,
        min_train_trades=args.min_train_trades,
        warmup_candles=args.warmup_candles,
        allow_partial=args.allow_partial,
    )


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default="data/market.sqlite")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", required=True)
    parser.add_argument("--since", required=True, help="UTC date, YYYY-MM-DD")
    parser.add_argument("--until", required=True, help="UTC date, YYYY-MM-DD, exclusive")
    parser.add_argument("--cash", default="1000")
    parser.add_argument("--fee-bps", default="10")
    parser.add_argument("--slippage-bps", default="5")
    parser.add_argument("--position-fraction", default="0.25")
    parser.add_argument("--min-notional", default="10")
    parser.add_argument("--quantity-step", default="0.00001")
    parser.add_argument("--min-candles", type=int, default=100)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m nomicronix.backtesting")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run one backtest")
    _add_common(run)
    run.add_argument("--strategy", required=True, help="module:Class")
    run.add_argument("--param", action="append", default=None, help="KEY=VALUE, repeatable")
    run.add_argument("--out", default=None)
    run.add_argument("--force", action="store_true")
    run.set_defaults(handler="run")

    wf = commands.add_parser("walk-forward", help="run walk-forward validation")
    _add_common(wf)
    wf.add_argument("--strategy", required=True, help="module:Class")
    wf.add_argument("--grid", action="append", default=None, help="KEY=V1,V2, repeatable")
    wf.add_argument("--train", default="90d")
    wf.add_argument("--test", default="30d")
    wf.add_argument("--step", default="30d")
    wf.add_argument("--objective", default="sharpe", choices=["sharpe", "total_return", "calmar"])
    wf.add_argument("--min-train-trades", type=int, default=10)
    wf.add_argument("--warmup-candles", type=int, default=200)
    wf.add_argument("--allow-partial", action="store_true")
    wf.add_argument("--out", default=None)
    wf.add_argument("--force", action="store_true")
    wf.set_defaults(handler="walk-forward")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    store: SqliteStore | None = None
    try:
        config = _config(args)
        store = SqliteStore(args.db, args.exchange)
        strategy_class = _load_strategy(args.strategy)
        factory = _factory(strategy_class)

        if args.handler == "run":
            result: BacktestResult | WalkForwardResult = run_backtest(
                store,
                strategy_class(**_parse_params(args.param)),
                config,
            )
        else:
            result = run_walk_forward(
                store,
                factory,
                _parse_grid(args.grid),
                config,
                _walk_forward_config(args),
            )

        print(summary(result))
        if args.out:
            destination = write_result(result, args.out, force=args.force)
            print(f"wrote {destination}")
        return 0
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except (DataError, WalkForwardError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (AttributeError, ImportError, TypeError) as exc:
        print(f"strategy error: {exc}", file=sys.stderr)
        return 2
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
