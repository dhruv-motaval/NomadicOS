"""Fixtures for Phase 14B benchmark tests (see helpers.py for builders)."""

from __future__ import annotations

from pathlib import Path

import pytest
from bench_helpers import make_app


@pytest.fixture()
def benchmark_app(tmp_path: Path):
    return make_app(tmp_path)
