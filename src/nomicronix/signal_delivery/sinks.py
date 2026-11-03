"""Delivery sinks: console, append-only JSONL and Telegram."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, TextIO
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from nomicronix.signal_delivery.models import (
    ConfigError,
    DeliveryError,
    Notification,
)
from nomicronix.signal_delivery.render import render_json, render_text

TOKEN_ENV = "NOMICRONIX_TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV = "NOMICRONIX_TELEGRAM_CHAT_ID"


class Sink(Protocol):
    name: str

    def send(self, notification: Notification) -> None:
        """Deliver one notification or raise `DeliveryError`."""
        ...


class ConsoleSink:
    name = "console"

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = sys.stdout if stream is None else stream

    def send(self, notification: Notification) -> None:
        print(render_text(notification), file=self._stream)


class JsonlSink:
    name = "jsonl"

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def send(self, notification: Notification) -> None:
        line = render_json(notification) + "\n"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
        except OSError as exc:
            raise DeliveryError(f"jsonl sink failed: {exc}") from exc


def _urllib_transport(token: str, chat_id: str) -> Callable[[str], None]:
    def send(text: str) -> None:
        data = urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:  # noqa: S310 - fixed Telegram host
                raw = response.read()
        except (OSError, ValueError) as exc:
            # Never include the request URL: it contains the bot token.
            raise DeliveryError("telegram request failed") from exc
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise DeliveryError("telegram returned invalid JSON") from exc
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise DeliveryError("telegram rejected the message")

    return send


class TelegramSink:
    name = "telegram"

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        transport: Callable[[str], None] | None = None,
    ) -> None:
        if not token.strip() or not chat_id.strip():
            raise ConfigError("telegram requires a bot token and a chat id")
        self._token = token
        self._chat_id = chat_id
        self._transport = _urllib_transport(token, chat_id) if transport is None else transport

    @classmethod
    def from_env(
        cls,
        *,
        environ: dict[str, str] | None = None,
        transport: Callable[[str], None] | None = None,
    ) -> TelegramSink:
        values = os.environ if environ is None else environ
        token = values.get(TOKEN_ENV, "")
        chat_id = values.get(CHAT_ID_ENV, "")
        if not token or not chat_id:
            raise ConfigError(f"telegram needs {TOKEN_ENV} and {CHAT_ID_ENV} in the environment")
        return cls(token, chat_id, transport=transport)

    def send(self, notification: Notification) -> None:
        self._transport(render_text(notification))
