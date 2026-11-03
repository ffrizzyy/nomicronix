from decimal import Decimal
from pathlib import Path

import pytest

from nomicronix.signal_delivery.config import (
    DeliveryDefaults,
    load_delivery_defaults,
    load_promotion_criteria,
)
from nomicronix.signal_delivery.models import ConfigError
from nomicronix.signal_delivery.readiness import PromotionCriteria


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


def test_repository_promotion_defaults_load() -> None:
    assert load_promotion_criteria(Path("config/delivery.toml")) == PromotionCriteria()


def test_missing_default_config_uses_promotion_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_promotion_criteria() == PromotionCriteria()


def test_missing_explicit_config_for_promotion_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_promotion_criteria(tmp_path / "missing.toml")


def test_promotion_overrides_load(tmp_path: Path) -> None:
    path = write_config(
        tmp_path / "delivery.toml",
        "[promotion]\n"
        "min_closed_trades = 10\n"
        "min_days = 7\n"
        'assumed_round_trip_cost_pct = "0.001"\n'
        'max_paper_drawdown_pct = "0.02"\n',
    )
    assert load_promotion_criteria(path) == PromotionCriteria(
        min_closed_trades=10,
        min_days=7,
        assumed_round_trip_cost_pct=Decimal("0.001"),
        max_paper_drawdown_pct=Decimal("0.02"),
    )


def test_absent_promotion_table_uses_defaults(tmp_path: Path) -> None:
    path = write_config(tmp_path / "delivery.toml", '[delivery]\nsinks = ["console"]\n')
    assert load_promotion_criteria(path) == PromotionCriteria()


@pytest.mark.parametrize(
    "body",
    [
        "[promotion]\nmin_closed_trades = 2.5\n",
        "[promotion]\nmin_closed_trades = true\n",
        '[promotion]\nmin_days = "30"\n',
        '[promotion]\nassumed_round_trip_cost_pct = "nope"\n',
        '[promotion]\nmax_paper_drawdown_pct = "1.5"\n',
        '[promotion]\nmax_paper_drawdown_pct = "0"\n',
        "[promotion]\nmin_closed_trades = 0\n",
        'promotion = "nope"\n',
        "[promotion\n",
    ],
)
def test_invalid_promotion_config_is_rejected(tmp_path: Path, body: str) -> None:
    path = write_config(tmp_path / "delivery.toml", body)
    with pytest.raises(ConfigError):
        load_promotion_criteria(path)
