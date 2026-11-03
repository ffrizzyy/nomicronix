"""Shared fixtures for signal-delivery tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.signal_delivery.fakes import Harness, build_harness


@pytest.fixture
def harness() -> Iterator[Harness]:
    active = build_harness()
    yield active
    active.close()
