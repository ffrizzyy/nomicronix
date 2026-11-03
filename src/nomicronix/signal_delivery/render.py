"""Deterministic notification rendering for every delivery sink."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from nomicronix.signal_delivery.models import Notification
from nomicronix.strategy_engine.models import decimal_text


def notification_payload(notification: Notification) -> dict[str, Any]:
    """A complete, JSON-safe payload that can rebuild the notification."""
    return {
        "schema_version": 1,
        "kind": "signal-notification",
        "signal_id": notification.signal_id,
        "strategy_id": notification.strategy_id,
        "symbol": notification.symbol,
        "timeframe": notification.timeframe,
        "side": notification.side,
        "entry_price": decimal_text(notification.entry_price),
        "stop_price": decimal_text(notification.stop_price),
        "target_price": decimal_text(notification.target_price),
        "confidence": notification.confidence,
        "rationale": notification.rationale,
        "created_at_ms": notification.created_at_ms,
        "expires_at_ms": notification.expires_at_ms,
        "approved": notification.approved,
        "approved_quantity": decimal_text(notification.approved_quantity),
        "approved_notional": decimal_text(notification.approved_notional),
        "risk_amount": decimal_text(notification.risk_amount),
        "reward_risk": decimal_text(notification.reward_risk),
        "reasons": list(notification.reasons),
    }


def notification_from_payload(payload: dict[str, Any]) -> Notification:
    """Rebuild a notification from an outbox row, validating it again."""
    reasons = payload.get("reasons", [])
    if not isinstance(reasons, list):
        raise ValueError("notification reasons must be a list")
    return Notification(
        signal_id=str(payload["signal_id"]),
        strategy_id=str(payload["strategy_id"]),
        symbol=str(payload["symbol"]),
        timeframe=str(payload["timeframe"]),
        side=str(payload["side"]),
        entry_price=Decimal(str(payload["entry_price"])),
        stop_price=Decimal(str(payload["stop_price"])),
        target_price=Decimal(str(payload["target_price"])),
        confidence=float(payload["confidence"]),
        rationale=str(payload["rationale"]),
        created_at_ms=int(payload["created_at_ms"]),
        expires_at_ms=int(payload["expires_at_ms"]),
        approved=bool(payload["approved"]),
        approved_quantity=Decimal(str(payload["approved_quantity"])),
        approved_notional=Decimal(str(payload["approved_notional"])),
        risk_amount=Decimal(str(payload["risk_amount"])),
        reward_risk=Decimal(str(payload["reward_risk"])),
        reasons=tuple(str(reason) for reason in reasons),
    )


def render_json(notification: Notification) -> str:
    """One compact, sorted-key JSON object, without a trailing newline."""
    return json.dumps(
        notification_payload(notification),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def render_text(notification: Notification) -> str:
    """A stable human-readable message for console and Telegram."""
    lines = [
        f"[SIGNAL {notification.status_text.upper()}] {notification.signal_id}",
        f"strategy {notification.strategy_id} | {notification.side} "
        f"{notification.symbol} {notification.timeframe}",
        f"entry {decimal_text(notification.entry_price)} | "
        f"stop {decimal_text(notification.stop_price)} | "
        f"target {decimal_text(notification.target_price)} | "
        f"confidence {notification.confidence:.2f}",
    ]
    if notification.approved:
        lines.append(
            f"qty {decimal_text(notification.approved_quantity)} | "
            f"notional {decimal_text(notification.approved_notional)} | "
            f"risk {decimal_text(notification.risk_amount)} | "
            f"R:R {decimal_text(notification.reward_risk)}"
        )
    else:
        lines.append(f"reasons: {', '.join(notification.reasons)}")
    lines.append(f"candle {notification.created_at_ms} | expires {notification.expires_at_ms}")
    lines.append(f"why: {notification.rationale}")
    return "\n".join(lines)
