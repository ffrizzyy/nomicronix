"""Load and validate delivery defaults from TOML."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from nomicronix.signal_delivery.models import ConfigError, InstrumentRules
from nomicronix.signal_delivery.readiness import PromotionCriteria

DEFAULT_CONFIG_PATH = Path("config/delivery.toml")
KNOWN_SINKS = frozenset({"console", "jsonl", "telegram"})


@dataclass(frozen=True, slots=True)
class DeliveryDefaults:
    sinks: tuple[str, ...] = ("console",)
    jsonl_path: str = "data/deliveries.jsonl"
    quantity_step: Decimal = Decimal("0.00001")
    min_quantity: Decimal = Decimal("0.00001")
    min_notional: Decimal = Decimal("10")

    def __post_init__(self) -> None:
        if not self.sinks:
            raise ConfigError("at least one delivery sink is required")
        if len(set(self.sinks)) != len(self.sinks):
            raise ConfigError("delivery sinks must not repeat")
        unknown = set(self.sinks) - KNOWN_SINKS
        if unknown:
            raise ConfigError(f"unknown delivery sinks: {sorted(unknown)}")
        if not self.jsonl_path.strip():
            raise ConfigError("jsonl_path must not be empty")
        InstrumentRules(
            quantity_step=self.quantity_step,
            min_quantity=self.min_quantity,
            min_notional=self.min_notional,
        )

    def instrument_rules(self) -> InstrumentRules:
        return InstrumentRules(
            quantity_step=self.quantity_step,
            min_quantity=self.min_quantity,
            min_notional=self.min_notional,
        )


def _sinks(section: dict[str, Any], default: tuple[str, ...]) -> tuple[str, ...]:
    raw = section.get("sinks", list(default))
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ConfigError("sinks must be a list of strings")
    return tuple(raw)


def _decimal(section: dict[str, Any], key: str, default: Decimal) -> Decimal:
    return Decimal(str(section.get(key, default)))


def load_delivery_defaults(path: str | Path | None = None) -> DeliveryDefaults:
    config_path = DEFAULT_CONFIG_PATH if path is None else Path(path)
    if not config_path.exists():
        if path is None:
            return DeliveryDefaults()
        raise ConfigError(f"delivery config not found: {config_path}")

    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    section = data.get("delivery")
    if not isinstance(section, dict):
        raise ConfigError("config must contain a [delivery] table")

    default = DeliveryDefaults()
    try:
        return DeliveryDefaults(
            sinks=_sinks(section, default.sinks),
            jsonl_path=str(section.get("jsonl_path", default.jsonl_path)),
            quantity_step=_decimal(section, "quantity_step", default.quantity_step),
            min_quantity=_decimal(section, "min_quantity", default.min_quantity),
            min_notional=_decimal(section, "min_notional", default.min_notional),
        )
    except (ConfigError, TypeError, ValueError, ArithmeticError) as exc:
        raise ConfigError(f"invalid delivery config in {config_path}: {exc}") from exc


def _int(section: dict[str, Any], key: str, default: int) -> int:
    raw = section.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(f"{key} must be an integer")
    return raw


def load_promotion_criteria(path: str | Path | None = None) -> PromotionCriteria:
    """Load the `[promotion]` table, falling back to the spec thresholds."""
    config_path = DEFAULT_CONFIG_PATH if path is None else Path(path)
    if not config_path.exists():
        if path is None:
            return PromotionCriteria()
        raise ConfigError(f"delivery config not found: {config_path}")

    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    section = data.get("promotion")
    if section is None:
        return PromotionCriteria()
    if not isinstance(section, dict):
        raise ConfigError("[promotion] must be a table")

    default = PromotionCriteria()
    try:
        return PromotionCriteria(
            min_closed_trades=_int(section, "min_closed_trades", default.min_closed_trades),
            min_days=_int(section, "min_days", default.min_days),
            assumed_round_trip_cost_pct=_decimal(
                section,
                "assumed_round_trip_cost_pct",
                default.assumed_round_trip_cost_pct,
            ),
            max_paper_drawdown_pct=_decimal(
                section, "max_paper_drawdown_pct", default.max_paper_drawdown_pct
            ),
        )
    except (ConfigError, TypeError, ValueError, ArithmeticError) as exc:
        raise ConfigError(f"invalid promotion config in {config_path}: {exc}") from exc
