from decimal import Decimal
from pathlib import Path

import pytest

from nomicronix.signal_delivery.config import (
    DeliveryDefaults,
    load_delivery_defaults,
)
from nomicronix.signal_delivery.models import ConfigError


def write_config(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_default_config_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_delivery_defaults() == DeliveryDefaults()


def test_missing_explicit_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_delivery_defaults(tmp_path / "missing.toml")


def test_repository_config_loads() -> None:
    assert load_delivery_defaults(Path("config/delivery.toml")) == DeliveryDefaults()


def test_config_overrides_defaults(tmp_path: Path) -> None:
    path = write_config(
        tmp_path / "delivery.toml",
        "[delivery]\n"
        'sinks = ["jsonl", "console"]\n'
        'jsonl_path = "out/signals.jsonl"\n'
        'quantity_step = "0.001"\n'
        'min_quantity = "0.01"\n'
        'min_notional = "20"\n',
    )
    defaults = load_delivery_defaults(path)
    assert defaults == DeliveryDefaults(
        sinks=("jsonl", "console"),
        jsonl_path="out/signals.jsonl",
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.01"),
        min_notional=Decimal("20"),
    )


def test_malformed_toml_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path / "delivery.toml", "[delivery\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_delivery_defaults(path)


def test_missing_delivery_table_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path / "delivery.toml", "[other]\nx = 1\n")
    with pytest.raises(ConfigError, match=r"\[delivery\] table"):
        load_delivery_defaults(path)


@pytest.mark.parametrize(
    "body",
    [
        "[delivery]\nsinks = []\n",
        '[delivery]\nsinks = ["console", "console"]\n',
        '[delivery]\nsinks = ["email"]\n',
        '[delivery]\nsinks = "console"\n',
        '[delivery]\nsinks = ["console", 1]\n',
        '[delivery]\njsonl_path = "  "\n',
        '[delivery]\nquantity_step = "0"\n',
        '[delivery]\nquantity_step = "-0.1"\n',
        '[delivery]\nmin_quantity = "-1"\n',
        '[delivery]\nmin_notional = "-1"\n',
    ],
)
def test_invalid_values_are_rejected(tmp_path: Path, body: str) -> None:
    path = write_config(tmp_path / "delivery.toml", body)
    with pytest.raises(ConfigError):
        load_delivery_defaults(path)


def test_instrument_rules_mirror_the_defaults() -> None:
    rules = load_delivery_defaults(Path("config/delivery.toml")).instrument_rules()
    assert rules.quantity_step == Decimal("0.00001")
    assert rules.min_quantity == Decimal("0.00001")
    assert rules.min_notional == Decimal("10")
