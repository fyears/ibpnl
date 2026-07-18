# -*- coding: utf-8 -*-
"""Shared test fixtures.

The production default provider is the live IB connection (``--provider ib``),
but the test suite must never touch a real Gateway/TWS. Pin the provider to the
deterministic mock for every test, regardless of the shipped default.
"""

from __future__ import annotations

import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _force_mock_provider():
    prev = settings.data_provider
    settings.data_provider = "mock"
    try:
        yield
    finally:
        settings.data_provider = prev
