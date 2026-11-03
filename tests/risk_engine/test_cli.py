import json
from pathlib import Path

from nomicronix.risk_engine.__main__ import main
from tests.risk_engine.fakes import NOW_MS

CONFIG = str(Path(__file__).parents[2] / "config" / "risk.toml")


def proposal_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "signal_id": "sig-1",
        "strategy_id": "test-strategy",
        "symbol": "BTC/USDT",
        "side": "buy",
        "entry_price": "100",
        "stop_price": "95",
        "target_price": "115",
        "confidence": 0.8,
        "created_at_ms": NOW_MS,
        "instrument": {
            "symbol": "BTC/USDT",
            "quantity_step": "0.00001",
            "min_quantity": "0.00001",
            "min_notional": "10",
        },
    }
    data.update(overrides)
    return data


def write_json(path: Path, payload: object) -> str:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def base_args(tmp_path: Path) -> list[str]:
    return [
        "--db",
        str(tmp_path / "risk.sqlite"),
        "--config",
        CONFIG,
        "--now-ms",
        str(NOW_MS),
    ]


def test_status_initializes_a_paper_account(tmp_path, capsys) -> None:
    code = main(["status", *base_args(tmp_path)])
    assert code == 0
    output = capsys.readouterr().out
    assert '"equity": "1000"' in output
    assert '"trading_allowed": true' in output


def test_check_approves_and_returns_zero(tmp_path, capsys) -> None:
    proposal = write_json(tmp_path / "proposal.json", proposal_data())
    code = main(
        [
            "check",
            *base_args(tmp_path),
            "--proposal",
            proposal,
            "--price",
            "BTC/USDT=100",
        ]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert '"approved": true' in output
    assert '"approved_quantity": "1"' in output


def test_check_rejects_stale_signal_with_exit_two(tmp_path, capsys) -> None:
    proposal = write_json(
        tmp_path / "proposal.json",
        proposal_data(created_at_ms=NOW_MS - 600_000),
    )
    code = main(
        [
            "check",
            *base_args(tmp_path),
            "--proposal",
            proposal,
            "--price",
            "BTC/USDT=100",
        ]
    )
    assert code == 2
    output = capsys.readouterr().out
    assert "stale_signal" in output


def test_kill_blocks_checks_and_resume_requires_phrase(tmp_path, capsys) -> None:
    args = base_args(tmp_path)
    assert main(["kill", *args, "--reason", "manual stop"]) == 0
    capsys.readouterr()

    proposal = write_json(tmp_path / "proposal.json", proposal_data())
    assert main(["check", *args, "--proposal", proposal, "--price", "BTC/USDT=100"]) == 2
    assert "kill_switch" in capsys.readouterr().out

    assert main(["resume", *args, "--confirm", "resume trading"]) == 1
    assert "RESUME TRADING" in capsys.readouterr().err

    assert main(["resume", *args, "--confirm", "RESUME TRADING"]) == 0
    assert '"kill_switch": null' in capsys.readouterr().out


def test_mark_updates_equity(tmp_path, capsys) -> None:
    prices = write_json(tmp_path / "prices.json", {"BTC/USDT": "110"})
    assert main(["mark", *base_args(tmp_path), "--prices", prices]) == 0
    assert '"equity": "1000"' in capsys.readouterr().out


def test_invalid_proposal_returns_one(tmp_path, capsys) -> None:
    proposal = write_json(tmp_path / "proposal.json", {"signal_id": "incomplete"})
    assert main(["check", *base_args(tmp_path), "--proposal", proposal]) == 1
    assert "error:" in capsys.readouterr().err


def test_invalid_price_override_returns_one(tmp_path, capsys) -> None:
    proposal = write_json(tmp_path / "proposal.json", proposal_data())
    assert main(["check", *base_args(tmp_path), "--proposal", proposal, "--price", "bad"]) == 1
    assert "SYMBOL=PRICE" in capsys.readouterr().err
