"""Retry-wrapper tests (TRD-027-TEST).

The retry decorator is purely Python-level — no I/O, no
subprocesses — so all mocking is in-process.

Coverage:
* exactly N attempts on persistent failure
* short-circuit on success
* short-circuit on success-with-passed=True
* re-attempt on success-with-passed=False
* human-review label applied on final failure
* labelling failure swallowed (test still returns the last
  failed response)
* no labelling when no active issue
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from ghia import retry
from ghia.app import GhiaApp
from ghia.config import Config
from ghia.errors import ErrorCode, ToolResponse, err, ok
from ghia.integrations.github import GitHubClientError
from ghia.session import SessionStore


def _make_app(tmp_path: Path) -> GhiaApp:
    cfg = Config(
        token="ghp_" + "x" * 36,
        repo="octo/hello",
        label="ai-fix",
        mode="full",
        poll_interval_min=30,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    return GhiaApp(
        config=cfg,
        session=SessionStore(tmp_path / "session.json"),
        repo_root=repo,
        logger=logging.getLogger("ghia-test-retry"),
    )


class _LabelClient:
    """Minimal stand-in capturing add_label calls."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[tuple[int, str]] = []
        self.raises = raises

    async def add_label(self, number: int, label: str) -> None:
        self.calls.append((number, label))
        if self.raises is not None:
            raise self.raises


# ----------------------------------------------------------------------
# Attempt counting
# ----------------------------------------------------------------------


async def test_three_attempts_max_on_persistent_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)

    monkeypatch.setattr(retry, "_get_client", lambda _app: _LabelClient())

    counter = {"n": 0}

    @retry.with_retries(max_attempts=3)
    async def always_fail(_app: GhiaApp) -> ToolResponse:
        counter["n"] += 1
        return ok({"passed": False})

    resp = await always_fail(app)

    assert counter["n"] == 3
    assert resp.success
    assert resp.data["passed"] is False


async def test_success_short_circuits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    monkeypatch.setattr(retry, "_get_client", lambda _app: _LabelClient())

    counter = {"n": 0}

    @retry.with_retries(max_attempts=3)
    async def succeed_on_two(_app: GhiaApp) -> ToolResponse:
        counter["n"] += 1
        if counter["n"] == 2:
            return ok({"passed": True})
        return ok({"passed": False})

    resp = await succeed_on_two(app)
    assert counter["n"] == 2
    assert resp.success
    assert resp.data["passed"] is True


async def test_tool_error_response_treated_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An err(...) response is also a failure for retry purposes."""

    app = _make_app(tmp_path)
    monkeypatch.setattr(retry, "_get_client", lambda _app: _LabelClient())

    counter = {"n": 0}

    @retry.with_retries(max_attempts=2)
    async def always_err(_app: GhiaApp) -> ToolResponse:
        counter["n"] += 1
        return err(ErrorCode.TEST_FAILED, "infra is on fire")

    resp = await always_err(app)
    assert counter["n"] == 2
    assert not resp.success
    assert resp.code == ErrorCode.TEST_FAILED


async def test_success_without_passed_field_short_circuits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain success payload (no `passed` key) counts as success."""

    app = _make_app(tmp_path)
    monkeypatch.setattr(retry, "_get_client", lambda _app: _LabelClient())

    counter = {"n": 0}

    @retry.with_retries(max_attempts=3)
    async def first_try(_app: GhiaApp) -> ToolResponse:
        counter["n"] += 1
        return ok({"some": "data"})

    resp = await first_try(app)
    assert counter["n"] == 1
    assert resp.success


# ----------------------------------------------------------------------
# Labelling on final failure
# ----------------------------------------------------------------------


async def test_human_review_label_applied_on_final_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    await app.session.update(active_issue=42)

    fake = _LabelClient()
    monkeypatch.setattr(retry, "_get_client", lambda _app: fake)

    @retry.with_retries(max_attempts=3)
    async def fails(_app: GhiaApp) -> ToolResponse:
        return ok({"passed": False})

    resp = await fails(app)

    assert resp.success
    assert resp.data["passed"] is False
    assert fake.calls == [(42, "human-review")]


async def test_label_failure_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A labelling exception must NOT propagate to the caller."""

    app = _make_app(tmp_path)
    await app.session.update(active_issue=99)

    fake = _LabelClient(
        raises=GitHubClientError(ErrorCode.RATE_LIMITED, "slow down")
    )
    monkeypatch.setattr(retry, "_get_client", lambda _app: fake)

    @retry.with_retries(max_attempts=2)
    async def fails(_app: GhiaApp) -> ToolResponse:
        return ok({"passed": False})

    # Must not raise — the swallow guarantees the caller still gets
    # the retry's last response.
    resp = await fails(app)
    assert resp.success
    assert resp.data["passed"] is False
    assert fake.calls == [(99, "human-review")]


async def test_no_active_issue_skips_labelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    # active_issue stays None.

    fake = _LabelClient()
    monkeypatch.setattr(retry, "_get_client", lambda _app: fake)

    @retry.with_retries(max_attempts=2)
    async def fails(_app: GhiaApp) -> ToolResponse:
        return ok({"passed": False})

    await fails(app)
    assert fake.calls == []


async def test_success_does_not_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    await app.session.update(active_issue=7)

    fake = _LabelClient()
    monkeypatch.setattr(retry, "_get_client", lambda _app: fake)

    @retry.with_retries(max_attempts=3)
    async def succeed(_app: GhiaApp) -> ToolResponse:
        return ok({"passed": True})

    await succeed(app)
    assert fake.calls == []


# ----------------------------------------------------------------------
# Decorator hygiene
# ----------------------------------------------------------------------


def test_max_attempts_zero_rejected() -> None:
    with pytest.raises(ValueError):
        retry.with_retries(max_attempts=0)
