"""Shared fixtures for strategy-engine tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.strategy_engine.fakes import close_stores


@pytest.fixture(autouse=True)
def _close_test_stores() -> Iterator[None]:
    yield
    close_stores()
