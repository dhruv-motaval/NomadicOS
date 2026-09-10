"""Shared fixtures for the NomadicOS test suite."""

import pytest

from nomadicos.core.events import TraceContext

SRC_IN_PATH = True


@pytest.fixture()
def trace() -> TraceContext:
    return TraceContext(request_id="req-test", user_id="user-1", session_id="session-1")
