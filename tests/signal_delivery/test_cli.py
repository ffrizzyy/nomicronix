import json
from pathlib import Path

import pytest

from nomicronix.signal_delivery.__main__ import main
from nomicronix.signal_delivery.sinks import CHAT_ID_ENV, TOKEN_ENV
from tests.signal_delivery.fakes import make_candidate, write_signals


def storage(tmp_path: Path) -> list[str]:
    return [
        "--delivery-db",
        str(tmp_path / "delivery.sqlite"),
        "--risk-db",
        str(tmp_path / "risk.sqlite"),
    ]


def test_deliver_approved_signal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    signals = write_signals(tmp_path / "signals.json", [make_candidate()])

    assert (
        main(
            [
                "deliver",
                "--signals",
                str(signals),
                *storage(tmp_path),
                "--sink",
                "console",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "delivered: sma-cross:BTC/USDT:1h:" in out
    assert "delivered 1 | rejected 0 | skipped 0 | retried 0" in out
    assert "outbox: 1 total | delivered 1" in out


def test_deliver_is_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    signals = write_signals(tmp_path / "signals.json", [make_candidate()])
    args = ["deliver", "--signals", str(signals), *storage(tmp_path), "--sink", "console"]

    assert main(args) == 0
    capsys.readouterr()
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "skipped: sma-cross" in out
    assert "delivered 0 | rejected 0 | skipped 1 | retried 0" in out


def test_deliver_all_rejected_still_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    signals = write_signals(tmp_path / "signals.json", [make_candidate(stop="97", target="103")])

    assert (
        main(
            [
                "deliver",
                "--signals",
                str(signals),
                *storage(tmp_path),
                "--sink",
                "console",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "rejected: sma-cross" in out
    assert "reward_risk_too_low" in out
    assert "delivered 0 | rejected 1 | skipped 0 | retried 0" in out


def test_missing_signal_file_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "deliver",
                "--signals",
                str(tmp_path / "missing.json"),
                *storage(tmp_path),
                "--sink",
                "console",
            ]
        )
        == 1
    )
    assert "error:" in capsys.readouterr().err


def test_malformed_signal_file_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"kind": "wrong"}', encoding="utf-8")
    assert (
        main(
            [
                "deliver",
                "--signals",
                str(bad),
                *storage(tmp_path),
                "--sink",
                "console",
            ]
        )
        == 1
    )
    assert "schema_version" in capsys.readouterr().err


def test_telegram_without_credentials_returns_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(CHAT_ID_ENV, raising=False)
    signals = write_signals(tmp_path / "signals.json", [make_candidate()])

    assert (
        main(
            [
                "deliver",
                "--signals",
                str(signals),
                *storage(tmp_path),
                "--sink",
                "telegram",
            ]
        )
        == 1
    )
    assert "environment" in capsys.readouterr().err


def test_sink_failure_returns_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    signals = write_signals(tmp_path / "signals.json", [make_candidate()])
    directory = tmp_path / "a-directory"
    directory.mkdir()

    assert (
        main(
            [
                "deliver",
                "--signals",
                str(signals),
                *storage(tmp_path),
                "--sink",
                "jsonl",
                "--jsonl",
                str(directory),
            ]
        )
        == 2
    )
    assert "delivery error:" in capsys.readouterr().err


def test_tick_and_report_after_a_stop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    signal = make_candidate()
    signals = write_signals(tmp_path / "signals.json", [signal])
    assert (
        main(
            [
                "deliver",
                "--signals",
                str(signals),
                *storage(tmp_path),
                "--sink",
                "console",
            ]
        )
        == 0
    )
    capsys.readouterr()

    at_ms = signal.created_at_ms + 60_000
    assert (
        main(
            [
                "tick",
                "--price",
                "BTC/USDT=94",
                "--at",
                str(at_ms),
                *storage(tmp_path),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "closed sma-cross" in out
    assert "stop" in out
    assert "pnl -6" in out
    assert "closed: 1 | open: 0" in out

    assert main(["report", *storage(tmp_path)]) == 0
    report = capsys.readouterr().out
    assert "realized pnl: -6 USDT" in report
    assert "closed 1 | open 0" in report


def test_tick_reads_a_prices_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    signal = make_candidate()
    signals = write_signals(tmp_path / "signals.json", [signal])
    main(
        [
            "deliver",
            "--signals",
            str(signals),
            *storage(tmp_path),
            "--sink",
            "console",
        ]
    )
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"BTC/USDT": "116"}), encoding="utf-8")

    assert (
        main(
            [
                "tick",
                "--prices",
                str(prices),
                "--at",
                str(signal.created_at_ms + 60_000),
                *storage(tmp_path),
            ]
        )
        == 0
    )
    assert "target" in capsys.readouterr().out


@pytest.mark.parametrize(
    "extra",
    [
        ["--price", "BTC/USDT"],
        ["--price", "BTC/USDT=-1"],
        ["--at", "not-a-number"],
        [],
    ],
)
def test_bad_tick_arguments_return_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str]
) -> None:
    assert main(["tick", *storage(tmp_path), *extra]) == 1
    assert "error:" in capsys.readouterr().err


def test_missing_explicit_config_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    signals = write_signals(tmp_path / "signals.json", [make_candidate()])
    assert (
        main(
            [
                "deliver",
                "--signals",
                str(signals),
                *storage(tmp_path),
                "--config",
                str(tmp_path / "missing.toml"),
            ]
        )
        == 1
    )
    assert "not found" in capsys.readouterr().err


def test_report_on_an_empty_database_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["report", *storage(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "deliveries: 0 total" in out
    assert "win rate: n/a" in out


def test_repeated_sink_flags_are_deduplicated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    signals = write_signals(tmp_path / "signals.json", [make_candidate()])

    assert (
        main(
            [
                "deliver",
                "--signals",
                str(signals),
                *storage(tmp_path),
                "--sink",
                "console",
                "--sink",
                "console",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.count("[SIGNAL APPROVED]") == 1


def test_a_failed_delivery_is_retried_on_the_next_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    signal = make_candidate()
    signals = write_signals(tmp_path / "signals.json", [signal])
    directory = tmp_path / "a-directory"
    directory.mkdir()
    failing = [
        "deliver",
        "--signals",
        str(signals),
        *storage(tmp_path),
        "--sink",
        "jsonl",
        "--jsonl",
        str(directory),
    ]
    assert main(failing) == 2
    capsys.readouterr()

    jsonl = tmp_path / "deliveries.jsonl"
    assert (
        main(
            [
                "deliver",
                "--signals",
                str(signals),
                *storage(tmp_path),
                "--sink",
                "jsonl",
                "--jsonl",
                str(jsonl),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "retried: sma-cross" in out
    assert "delivered 0 | rejected 0 | skipped 0 | retried 1" in out
    assert jsonl.read_text(encoding="utf-8").count("\n") == 1


@pytest.mark.parametrize(
    "content",
    ["{", "[]", '{"BTC/USDT": "not-a-number"}', '{"BTC/USDT": "0"}'],
)
def test_bad_prices_file_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    prices = tmp_path / "prices.json"
    prices.write_text(content, encoding="utf-8")
    assert main(["tick", "--prices", str(prices), "--at", "1", *storage(tmp_path)]) == 1
    assert "error:" in capsys.readouterr().err


def test_missing_prices_file_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "tick",
                "--prices",
                str(tmp_path / "missing.json"),
                "--at",
                "1",
                *storage(tmp_path),
            ]
        )
        == 1
    )
    assert "cannot read prices file" in capsys.readouterr().err


def test_instrument_flags_override_the_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    signals = write_signals(tmp_path / "signals.json", [make_candidate()])

    assert (
        main(
            [
                "deliver",
                "--signals",
                str(signals),
                *storage(tmp_path),
                "--sink",
                "console",
                "--quantity-step",
                "0.5",
                "--min-quantity",
                "1",
                "--min-notional",
                "0",
            ]
        )
        == 0
    )
    assert "delivered 1" in capsys.readouterr().out


def test_build_sinks_rejects_empty_and_unknown_names() -> None:
    from nomicronix.signal_delivery.__main__ import _build_sinks
    from nomicronix.signal_delivery.config import DeliveryDefaults
    from nomicronix.signal_delivery.models import ConfigError

    with pytest.raises(ConfigError, match="at least one"):
        _build_sinks([], DeliveryDefaults(), None)
    with pytest.raises(ConfigError, match="unknown sink"):
        _build_sinks(["email"], DeliveryDefaults(), None)


def test_outcome_line_reports_errors_and_survives_corrupt_payloads() -> None:
    from dataclasses import replace

    from nomicronix.signal_delivery.__main__ import _outcome_line
    from nomicronix.signal_delivery.models import (
        DeliveryAction,
        DeliveryOutcome,
        DeliveryRecord,
        DeliveryStatus,
    )

    record = DeliveryRecord(
        signal_id="signal-1",
        status=DeliveryStatus.REJECTED,
        payload_json="{",
        recorded_at_ms=1,
    )
    assert (
        _outcome_line(DeliveryOutcome(record=record, action=DeliveryAction.REJECTED))
        == "rejected: signal-1"
    )

    failed = replace(record, status=DeliveryStatus.FAILED, error="sink down")
    assert (
        _outcome_line(DeliveryOutcome(record=failed, action=DeliveryAction.RETRIED))
        == "retried: signal-1 (sink down)"
    )


def test_bad_inline_price_value_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["tick", "--price", "BTC/USDT=abc", "--at", "1", *storage(tmp_path)]) == 1
    assert "not a decimal number" in capsys.readouterr().err
