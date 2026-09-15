"""Fixtures for orchestration tests (see helpers.py for builders)."""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import make_app


@pytest.fixture()
def orchestration(tmp_path: Path):
    return make_app(tmp_path)
