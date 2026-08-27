"""Shared fixture loading for the transport tests."""
from pathlib import Path

import pytest

# tests/ -> mud-control/ -> services/ -> repo root
FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "tintin"


def load(name: str) -> bytes:
    path = FIXTURE_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"missing transport fixture: {path}")
    return path.read_bytes()


@pytest.fixture(scope="session")
def fixtures():
    return load
