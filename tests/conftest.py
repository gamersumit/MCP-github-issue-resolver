"""Shared pytest fixtures for the ghia test suite."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, List

import pytest

from ghia import redaction


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A temp directory that stands in for a repo root in path-guard tests."""

    root = tmp_path / "repo"
    root.mkdir()
    return root


class _ListHandler(logging.Handler):
    """Simple handler that captures formatted records in a list."""

    def __init__(self) -> None:
        super().__init__()
        self.records: List[logging.LogRecord] = []
        self.messages: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        try:
            self.messages.append(self.format(record))
        except Exception:  # noqa: BLE001 — mirror logging's own tolerance
            self.messages.append(record.getMessage())


@pytest.fixture
def captured_logger() -> Iterator[_ListHandler]:
    """Yield a list-handler attached to the root logger with redaction on.

    The handler collects both :class:`logging.LogRecord` instances and
    already-formatted message strings so tests can assert on either.
    Both the root logger level and the handler level are pulled down to
    DEBUG so tests can exercise all severity branches.
    """

    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.DEBUG)

    handler = _ListHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    # Attach the redaction filter to the handler so it fires even if a
    # higher-level config exists in the environment.
    handler.addFilter(redaction.RedactionFilter())
    root.addHandler(handler)

    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
        # Clear any token registration left over from a test.
        redaction.set_token(None)


class _MockClock:
    """Tiny controllable clock for deterministic timestamp tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def advance(self, dt: float) -> None:
        self.now += dt

    def time(self) -> float:
        return self.now


@pytest.fixture
def mock_clock() -> _MockClock:
    """A controllable clock callers can advance manually."""

    return _MockClock()
