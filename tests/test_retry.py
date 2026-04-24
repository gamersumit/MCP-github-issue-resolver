"""Retry-wrapper tests (TRD-027-TEST, v0.2 refactor).

The retry decorator is purely Python-level — no I/O, no
subprocesses — so all mocking is in-process.  v0.2 swap: the labelling
side-effect now goes through ``gh_cli.add_label`` directly (no
``_get_client`` middleman) so we patch that coroutine.

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
from ghia.integrations import gh_cli
from ghia.integrations.gh_cli import GhAuthError
from ghia.session import SessionStore


def _make_app(tmp_path: Path) -> GhiaApp:
    cfg = Config(
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
        repo_full_name="octo/hello",
        logger=logging.getLogger("ghia-test-retry"),
    )


def _patch_add_label(
    monkeypatch: pytest.MonkeyPatch, *, raises: Exception | None = None
) -> list[tuple[str, int, str]]:
    """Mock gh_cli.add_label and return the captured call list."""

    calls: list[tuple[str, int, str]] = []

    async def fake_add_label(repo: str, number: int, label: str) -> None:
        calls.append((repo, number, label))
        if raises is not None:
            raise raises

    monkeypatch.setattr(gh_cli, "add_label", fake_add_label)
    return calls


# ----------------------------------------------------------------------
# Attempt counting
# ----------------------------------------------------------------------


async def test_three_attempts_max_on_persistent_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    _patch_add_label(monkeypatch)

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
    _patch_add_label(monkeypatch)

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
    _patch_add_label(monkeypatch)

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
    _patch_add_label(monkeypatch)

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

    calls = _patch_add_label(monkeypatch)

    @retry.with_retries(max_attempts=3)
    async def fails(_app: GhiaApp) -> ToolResponse:
        return ok({"passed": False})

    resp = await fails(app)

    assert resp.success
    assert resp.data["passed"] is False
    assert calls == [("octo/hello", 42, "human-review")]


async def test_label_failure_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A labelling exception must NOT propagate to the caller."""

    app = _make_app(tmp_path)
    await app.session.update(active_issue=99)

    calls = _patch_add_label(
        monkeypatch,
        raises=GhAuthError(ErrorCode.RATE_LIMITED, "slow down"),
    )

    @retry.with_retries(max_attempts=2)
    async def fails(_app: GhiaApp) -> ToolResponse:
        return ok({"passed": False})

    # Must not raise — the swallow guarantees the caller still gets
    # the retry's last response.
    resp = await fails(app)
    assert resp.success
    assert resp.data["passed"] is False
    assert calls == [("octo/hello", 99, "human-review")]


async def test_no_active_issue_skips_labelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    # active_issue stays None.

    calls = _patch_add_label(monkeypatch)

    @retry.with_retries(max_attempts=2)
    async def fails(_app: GhiaApp) -> ToolResponse:
        return ok({"passed": False})

    await fails(app)
    assert calls == []


async def test_success_does_not_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    await app.session.update(active_issue=7)

    calls = _patch_add_label(monkeypatch)

    @retry.with_retries(max_attempts=3)
    async def succeed(_app: GhiaApp) -> ToolResponse:
        return ok({"passed": True})

    await succeed(app)
    assert calls == []


# ----------------------------------------------------------------------
# Decorator hygiene
# ----------------------------------------------------------------------


def test_max_attempts_zero_rejected() -> None:
    with pytest.raises(ValueError):
        retry.with_retries(max_attempts=0)
