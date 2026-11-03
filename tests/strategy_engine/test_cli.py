import json
from pathlib import Path

import pytest

from nomicronix.market_data.store import SqliteStore
from nomicronix.strategy_engine.__main__ import (
    _parse_value,
    main,
    parse_duration,
    parse_time,
)
from tests.strategy_engine.fakes import HOUR_MS, JAN_1_2025_MS, make_candles

FIXED = "tests.strategy_engine.fakes:FixedProposer"
FLAT = "tests.strategy_engine.fakes:AlwaysFlat"


def build_db(path: Path, count: int = 48) -> Path:
    store = SqliteStore(str(path), "binance")
    store.upsert_candles(make_candles(count, start_ms=JAN_1_2025_MS))
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
        "2025-01-02",
        "--min-history",
        "1",
    ]


def test_parse_time_accepts_dates_and_epoch_milliseconds() -> None:
    assert parse_time("2025-01-01") == JAN_1_2025_MS
    assert parse_time(str(JAN_1_2025_MS)) == JAN_1_2025_MS


@pytest.mark.parametrize("value", ["30s", "5m", "12h", "1d"])
def test_parse_duration_units(value: str) -> None:
    units = {"s": 1_000, "m": 60_000, "h": HOUR_MS, "d": 86_400_000}
    assert parse_duration(value) == int(value[:-1]) * units[value[-1]]


@pytest.mark.parametrize("value", ["30", "h", "30x", "1.5h", ""])
def test_parse_duration_rejects_bad_input(value: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(value)


def test_param_values_parse_as_int_then_float_then_string() -> None:
    assert _parse_value("20") == 20
    assert isinstance(_parse_value("20"), int)
    assert _parse_value("2.5") == 2.5
    assert isinstance(_parse_value("2.5"), float)
    assert _parse_value("hello") == "hello"
    assert _parse_value("") == ""


def test_string_params_reach_the_proposer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = build_db(tmp_path / "market.sqlite")
    args = [
        "scan",
        *base_args(db),
        "--strategy",
        "tests.strategy_engine.fakes:ParamProposer",
        "--param",
        "label=hello",
    ]
    assert main(args) == 0
    assert "signals: 0" in capsys.readouterr().out


def test_scan_writes_a_deterministic_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = build_db(tmp_path / "market.sqlite")
    out = tmp_path / "results" / "signals.json"

    assert (
        main(
            [
                "scan",
                *base_args(db),
                "--strategy",
                FIXED,
                "--out",
                str(out),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "signals: 24" in output
    assert f"wrote {out}" in output

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["kind"] == "signals"
    assert payload["data"]["candle_count"] == 24
    assert len(payload["signals"]) == 24


def test_zero_signals_still_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = build_db(tmp_path / "market.sqlite")
    assert main(["scan", *base_args(db), "--strategy", FLAT]) == 0
    assert "signals: 0" in capsys.readouterr().out


def test_existing_output_returns_three_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = build_db(tmp_path / "market.sqlite")
    out = tmp_path / "signals.json"
    args = ["scan", *base_args(db), "--strategy", FIXED, "--out", str(out)]

    assert main(args) == 0
    capsys.readouterr()
    assert main(args) == 3
    assert "already exists" in capsys.readouterr().err

    assert main([*args, "--force"]) == 0


def test_missing_strategy_module_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = build_db(tmp_path / "market.sqlite")
    assert main(["scan", *base_args(db), "--strategy", "does.not.exist:Nope"]) == 2
    assert "strategy error:" in capsys.readouterr().err


def test_missing_strategy_class_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = build_db(tmp_path / "market.sqlite")
    assert main(["scan", *base_args(db), "--strategy", "tests.strategy_engine.fakes:Nope"]) == 2
    assert "strategy error:" in capsys.readouterr().err


def test_malformed_strategy_spec_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = build_db(tmp_path / "market.sqlite")
    assert main(["scan", *base_args(db), "--strategy", "not-a-spec"]) == 2
    assert "module:Class" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("class_name", "message"),
    [
        ("NOT_A_CLASS", "is not a class"),
        ("NoStrategyId", "strategy_id"),
        ("BadMinHistory", "min_history"),
        ("NoPropose", "propose"),
    ],
)
def test_invalid_proposer_classes_return_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    class_name: str,
    message: str,
) -> None:
    db = build_db(tmp_path / "market.sqlite")
    spec = f"tests.strategy_engine.fakes:{class_name}"
    assert main(["scan", *base_args(db), "--strategy", spec]) == 2
    assert message in capsys.readouterr().err


def test_unknown_constructor_param_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = build_db(tmp_path / "market.sqlite")
    assert (
        main(
            [
                "scan",
                *base_args(db),
                "--strategy",
                FIXED,
                "--param",
                "unknown=1",
            ]
        )
        == 2
    )
    assert "strategy error:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [
        ["--param", "bad"],
        ["--min-confidence", "2"],
        ["--ttl", "0m"],
        ["--config", "missing.toml"],
    ],
)
def test_bad_arguments_return_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str]
) -> None:
    db = build_db(tmp_path / "market.sqlite")
    assert main(["scan", *base_args(db), "--strategy", FIXED, *extra]) == 1
    assert "error:" in capsys.readouterr().err


def test_bad_date_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = build_db(tmp_path / "market.sqlite")
    args = base_args(db)
    args[args.index("2025-01-01")] = "not-a-date"
    assert main(["scan", *args, "--strategy", FIXED]) == 1
    assert "error:" in capsys.readouterr().err


def test_gap_in_data_returns_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "market.sqlite"
    candles = make_candles(48, start_ms=JAN_1_2025_MS)
    del candles[10]
    store = SqliteStore(str(db), "binance")
    store.upsert_candles(candles)
    store.close()

    assert main(["scan", *base_args(db), "--strategy", FIXED]) == 1
    assert "gaps" in capsys.readouterr().err
