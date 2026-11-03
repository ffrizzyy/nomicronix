import json
from dataclasses import replace

import pytest

from nomicronix.signal_delivery.render import (
    notification_from_payload,
    notification_payload,
    render_json,
    render_text,
)
from tests.signal_delivery.fakes import make_notification


def test_approved_text_has_levels_size_and_rationale() -> None:
    text = render_text(make_notification())
    assert text.startswith("[SIGNAL APPROVED] sma-cross:BTC/USDT:1h:")
    assert "strategy sma-cross | buy BTC/USDT 1h" in text
    assert "entry 100 | stop 95 | target 115 | confidence 0.60" in text
    assert "qty 1 | notional 100 | risk 5 | R:R 3" in text
    assert "why: test signal" in text


def test_rejected_text_lists_reasons_and_no_size() -> None:
    text = render_text(make_notification(approved=False))
    assert text.startswith("[SIGNAL REJECTED]")
    assert "reasons: reward_risk_too_low" in text
    assert "qty" not in text


def test_payload_round_trips_exactly() -> None:
    notification = make_notification()
    payload = notification_payload(notification)
    assert notification_from_payload(payload) == notification

    rejected = make_notification(approved=False)
    assert notification_from_payload(notification_payload(rejected)) == rejected


def test_json_is_compact_and_sorted() -> None:
    notification = make_notification()
    text = render_json(notification)
    expected = json.dumps(
        notification_payload(notification),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert text == expected
    assert text.startswith('{"approved":')
    assert '": ' not in text
    assert ', "' not in text


def test_json_has_no_wall_clock_or_paths() -> None:
    payload = notification_payload(make_notification())
    assert "recorded_at_ms" not in payload
    assert "path" not in payload
    assert payload["kind"] == "signal-notification"


@pytest.mark.parametrize(
    "overrides",
    [
        {"approved": True, "reasons": ("x",)},
        {"approved": False, "reasons": ()},
        {"confidence": 1.5},
        {"stop_price": make_notification().entry_price},
        {"target_price": make_notification().entry_price},
        {"rationale": "  "},
        {"signal_id": ""},
        {"expires_at_ms": make_notification().created_at_ms},
        {"approved_quantity": make_notification().approved_quantity * -1},
    ],
)
def test_invalid_notification_is_rejected(overrides: dict[str, object]) -> None:
    payload = notification_payload(make_notification())
    payload.update(overrides)
    with pytest.raises((ValueError, KeyError, TypeError)):
        notification_from_payload(payload)


def test_notification_requires_reasons_when_rejected() -> None:
    with pytest.raises(ValueError, match="reason"):
        replace(make_notification(approved=False), reasons=())
