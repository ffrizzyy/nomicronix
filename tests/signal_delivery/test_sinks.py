import io
import json
from pathlib import Path

import pytest

from nomicronix.signal_delivery import sinks as sinks_module
from nomicronix.signal_delivery.models import ConfigError, DeliveryError
from nomicronix.signal_delivery.render import render_text
from nomicronix.signal_delivery.sinks import (
    CHAT_ID_ENV,
    TOKEN_ENV,
    ConsoleSink,
    JsonlSink,
    TelegramSink,
    _urllib_transport,
)
from tests.signal_delivery.fakes import make_notification


def test_console_sink_prints_rendered_text() -> None:
    stream = io.StringIO()
    notification = make_notification()
    ConsoleSink(stream).send(notification)
    assert stream.getvalue() == render_text(notification) + "\n"


def test_jsonl_sink_appends_one_sorted_object_per_line(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deliveries.jsonl"
    sink = JsonlSink(path)
    first = make_notification()
    second = make_notification(approved=False)

    sink.send(first)
    sink.send(second)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    payloads = [json.loads(line) for line in lines]
    assert [payload["signal_id"] for payload in payloads] == [
        first.signal_id,
        second.signal_id,
    ]
    assert list(payloads[0]) == sorted(payloads[0])
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_jsonl_sink_reports_io_errors(tmp_path: Path) -> None:
    sink = JsonlSink(tmp_path)
    with pytest.raises(DeliveryError, match="jsonl sink failed"):
        sink.send(make_notification())


def test_jsonl_sink_exposes_its_path(tmp_path: Path) -> None:
    assert JsonlSink(tmp_path / "out.jsonl").path == tmp_path / "out.jsonl"


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _capture_urlopen(monkeypatch: pytest.MonkeyPatch, body: bytes) -> list[object]:
    requests: list[object] = []

    def fake_urlopen(request: object, timeout: int) -> _Response:
        requests.append(request)
        return _Response(body)

    monkeypatch.setattr(sinks_module, "urlopen", fake_urlopen)
    return requests


def test_urllib_transport_posts_to_the_bot_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _capture_urlopen(monkeypatch, b'{"ok": true}')
    _urllib_transport("secret", "42")("hello")

    assert len(requests) == 1
    request = requests[0]
    assert request.full_url == "https://api.telegram.org/botsecret/sendMessage"
    assert b"chat_id=42" in request.data
    assert b"text=hello" in request.data


def test_urllib_transport_hides_the_token_on_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_urlopen(request: object, timeout: int) -> _Response:
        raise OSError("connection refused")

    monkeypatch.setattr(sinks_module, "urlopen", failing_urlopen)
    with pytest.raises(DeliveryError, match="telegram request failed") as info:
        _urllib_transport("secret-token", "42")("hello")
    assert "secret-token" not in str(info.value)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"not json", "invalid JSON"),
        (b'{"ok": false}', "rejected the message"),
        (b'["ok"]', "rejected the message"),
    ],
)
def test_urllib_transport_rejects_bad_responses(
    monkeypatch: pytest.MonkeyPatch, body: bytes, message: str
) -> None:
    _capture_urlopen(monkeypatch, body)
    with pytest.raises(DeliveryError, match=message):
        _urllib_transport("secret", "42")("hello")


def test_telegram_sink_uses_the_injected_transport() -> None:
    sent: list[str] = []
    sink = TelegramSink("token", "chat", transport=sent.append)
    notification = make_notification()

    sink.send(notification)

    assert sent == [render_text(notification)]


def test_telegram_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setenv(TOKEN_ENV, "token")
    monkeypatch.setenv(CHAT_ID_ENV, "chat")
    sink = TelegramSink.from_env(transport=sent.append)
    sink.send(make_notification())
    assert len(sent) == 1


def test_telegram_without_credentials_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(CHAT_ID_ENV, raising=False)
    with pytest.raises(ConfigError, match="environment"):
        TelegramSink.from_env()


def test_telegram_rejects_blank_credentials() -> None:
    with pytest.raises(ConfigError, match="bot token"):
        TelegramSink("", "chat")
    with pytest.raises(ConfigError, match="bot token"):
        TelegramSink("token", " ")
