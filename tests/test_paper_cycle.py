from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("paper_cycle", ROOT / "scripts" / "paper_cycle.py")
assert _SPEC is not None and _SPEC.loader is not None
paper_cycle = importlib.util.module_from_spec(_SPEC)
sys.modules["paper_cycle"] = paper_cycle
_SPEC.loader.exec_module(paper_cycle)


def plan(**overrides: object) -> object:
    values: dict[str, object] = {
        "symbols": ("BTC/USDT", "ETH/USDT"),
        "exchange": "binance",
        "timeframe": "1h",
        "since": "2026-01-01",
        "until_ms": 1_735_689_600_000,
        "strategy": "examples.strategy_models:SmaCross",
        "params": ("fast=20", "slow=50"),
        "min_confidence": "0.5",
        "ttl": "5m",
        "market_db": "data/market.sqlite",
        "delivery_db": "data/delivery.sqlite",
        "risk_db": "data/risk.sqlite",
        "results_dir": "results",
        "jsonl": "data/deliveries.jsonl",
        "cash": "1000",
        "sinks": ("console", "jsonl"),
        "prices": (("BTC/USDT", "100"),),
    }
    values.update(overrides)
    return paper_cycle.CyclePlan(**values)


def names(steps: list[object]) -> list[str]:
    return [step.name for step in steps]


def test_steps_run_in_a_causal_order() -> None:
    steps = paper_cycle.build_steps(plan())

    assert names(steps) == [
        "backfill",
        "tick",
        "scan BTC/USDT",
        "scan ETH/USDT",
        "deliver BTC/USDT",
        "deliver ETH/USDT",
        "readiness",
    ]


def test_tick_is_omitted_without_a_price() -> None:
    steps = paper_cycle.build_steps(plan(prices=()))

    assert "tick" not in names(steps)
    assert names(steps)[0] == "backfill"


def test_scan_and_deliver_carry_the_shared_databases() -> None:
    steps = paper_cycle.build_steps(plan())
    scan = next(step for step in steps if step.name == "scan BTC/USDT")
    deliver = next(step for step in steps if step.name == "deliver BTC/USDT")

    assert "--until" in scan.args
    assert str(1_735_689_600_000) in scan.args
    assert scan.args.count("--param") == 2
    assert "--force" in scan.args
    assert deliver.args.count("--sink") == 2
    assert "data/delivery.sqlite" in deliver.args
    assert "data/risk.sqlite" in deliver.args


def test_execute_stops_on_a_failed_step() -> None:
    steps = [
        paper_cycle.Step("backfill", ("-m",)),
        paper_cycle.Step("scan BTC/USDT", ("-m",)),
        paper_cycle.Step("readiness", ("-m",)),
    ]
    seen: list[str] = []

    def runner(step: object) -> int:
        seen.append(step.name)
        return 1 if step.name == "scan BTC/USDT" else 0

    assert paper_cycle.execute(steps, runner) == 1
    assert seen == ["backfill", "scan BTC/USDT"]


def test_execute_treats_readiness_two_as_a_report(capsys) -> None:
    steps = [paper_cycle.Step("readiness", ("-m",))]

    assert paper_cycle.execute(steps, lambda step: 2) == 0
    assert "still accumulating" in capsys.readouterr().out


def test_execute_fails_on_a_broken_readiness_command() -> None:
    steps = [paper_cycle.Step("readiness", ("-m",))]

    assert paper_cycle.execute(steps, lambda step: 1) == 1


def test_dry_run_prints_commands_without_writing(capsys) -> None:
    code = paper_cycle.main(["--dry-run", "--symbols", "BTC/USDT", "--since", "2026-01-01"])

    out = capsys.readouterr().out
    assert code == 0
    assert "nomicronix.market_data backfill" in out
    assert "nomicronix.strategy_engine scan" in out
    assert "nomicronix.signal_delivery readiness" in out


def test_a_bad_manual_price_is_rejected(capsys) -> None:
    code = paper_cycle.main(["--dry-run", "--price", "BTCUSDT"])

    assert code == 1
    assert "SYMBOL=PRICE" in capsys.readouterr().err


def test_an_unsupported_timeframe_is_rejected() -> None:
    with pytest.raises(SystemExit) as exit_code:
        paper_cycle.main(["--dry-run", "--timeframe", "3m"])

    assert exit_code.value.code == 2
