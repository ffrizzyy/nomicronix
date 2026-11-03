import json
from pathlib import Path

import pytest

from nomicronix.backtesting.__main__ import main, parse_date, parse_duration
from nomicronix.market_data.store import SqliteStore
from tests.backtesting.fakes import HOUR_MS, JAN_1_2025_MS, make_candles

STRATEGY = "tests.backtesting.fakes:EntryOnce"
BIAS_STRATEGY = "tests.backtesting.fakes:BiasEntry"


def build_db(path: Path, hours: int = 48) -> Path:
    store = SqliteStore(str(path), "binance")
    store.upsert_candles(make_candles(hours, start_ms=JAN_1_2025_MS))
    store.close()
    return path


def base_args(db: Path) -> list[str]:
    return [
        "--db",
        str(db),
        "--exchange",
        "binance",
        "--symbol",
        "BTC/USDT",
        "--timeframe",
        "1h",
        "--since",
        "2025-01-01",
        "--until",
        "2025-01-03",
        "--min-candles",
        "1",
    ]


def test_parse_date_is_utc() -> None:
    assert parse_date("2025-01-01") == JAN_1_2025_MS


def test_parse_duration_units() -> None:
    assert parse_duration("30m") == 30 * 60_000
    assert parse_duration("12h") == 12 * HOUR_MS
    assert parse_duration("90d") == 90 * 86_400_000


@pytest.mark.parametrize("value", ["30", "h", "30x", "1.5h", ""])
def test_parse_duration_rejects_bad_input(value: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(value)


def test_run_writes_a_deterministic_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = build_db(tmp_path / "market.sqlite")
    out = tmp_path / "results" / "run.json"

    assert (
        main(
            [
                "run",
                *base_args(db),
                "--strategy",
                STRATEGY,
                "--out",
                str(out),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "trades:" in output
    assert f"wrote {out}" in output

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["kind"] == "backtest"
    assert payload["data"]["candle_count"] == 48


def test_existing_output_returns_three_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = build_db(tmp_path / "market.sqlite")
    out = tmp_path / "run.json"
    args = ["run", *base_args(db), "--strategy", STRATEGY, "--out", str(out)]

    assert main(args) == 0
    capsys.readouterr()
    assert main(args) == 3
    assert "already exists" in capsys.readouterr().err

    assert main([*args, "--force"]) == 0


def test_run_without_output_only_prints_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = build_db(tmp_path / "market.sqlite")
    assert main(["run", *base_args(db), "--strategy", STRATEGY]) == 0
    assert "total return:" in capsys.readouterr().out


def test_walk_forward_writes_a_result(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = build_db(tmp_path / "market.sqlite")
    out = tmp_path / "walk.json"

    assert (
        main(
            [
                "walk-forward",
                *base_args(db),
                "--strategy",
                BIAS_STRATEGY,
                "--grid",
                "bias=0,1",
                "--train",
                "10h",
                "--test",
                "5h",
                "--step",
                "5h",
                "--min-train-trades",
                "1",
                "--warmup-candles",
                "0",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["kind"] == "walk_forward"
    assert len(payload["windows"]) == 7
    assert all(window["params"] == {"bias": 1} for window in payload["windows"])
    capsys.readouterr()


def test_missing_strategy_module_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = build_db(tmp_path / "market.sqlite")
    assert (
        main(
            [
                "run",
                *base_args(db),
                "--strategy",
                "does.not.exist:Nope",
            ]
        )
        == 2
    )
    assert "strategy error:" in capsys.readouterr().err


def test_missing_strategy_class_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = build_db(tmp_path / "market.sqlite")
    assert (
        main(
            [
                "run",
                *base_args(db),
                "--strategy",
                "tests.backtesting.fakes:NoSuchStrategy",
            ]
        )
        == 2
    )
    assert "strategy error:" in capsys.readouterr().err


def test_malformed_strategy_spec_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = build_db(tmp_path / "market.sqlite")
    assert main(["run", *base_args(db), "--strategy", "not-a-spec"]) == 1
    assert "module:Class" in capsys.readouterr().err


def test_bad_date_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = build_db(tmp_path / "market.sqlite")
    args = base_args(db)
    args[args.index("2025-01-01")] = "not-a-date"
    assert main(["run", *args, "--strategy", STRATEGY]) == 1
    assert "error:" in capsys.readouterr().err


def test_bad_grid_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = build_db(tmp_path / "market.sqlite")
    assert (
        main(
            [
                "walk-forward",
                *base_args(db),
                "--strategy",
                BIAS_STRATEGY,
                "--grid",
                "bad",
            ]
        )
        == 1
    )
    assert "error:" in capsys.readouterr().err


def test_bad_param_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = build_db(tmp_path / "market.sqlite")
    assert (
        main(
            [
                "run",
                *base_args(db),
                "--strategy",
                STRATEGY,
                "--param",
                "bad",
            ]
        )
        == 1
    )
    assert "error:" in capsys.readouterr().err


def test_gap_in_data_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "market.sqlite"
    candles = make_candles(48, start_ms=JAN_1_2025_MS)
    del candles[10]
    store = SqliteStore(str(db), "binance")
    store.upsert_candles(candles)
    store.close()

    assert main(["run", *base_args(db), "--strategy", STRATEGY]) == 1
    assert "gaps" in capsys.readouterr().err
