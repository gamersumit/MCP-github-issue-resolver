"""run_tests tool tests (TRD-026a/b-TEST — tool half).

These tests cover the orchestration layer that sits between the
tool envelope and the docker runner.  All Docker SDK contact is
mocked at the module boundary so the suite is hermetic.

Coverage:
* skipped when no test_command is configured
* DOCKER_UNAVAILABLE when daemon isn't reachable
* happy path: passed=True, structured payload
* test failure: ok-with-passed=False (NOT TEST_FAILED)
* timeout surfaces both passed=False AND timed_out=True
* TEST_FAILED only on mid-run docker death
* defense-in-depth: in-memory mutation past config validation
  still rejected
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from ghia.app import GhiaApp
from ghia.config import Config
from ghia.errors import ErrorCode
from ghia.integrations.docker_runner import DockerUnavailable
from ghia.session import SessionStore
from ghia.tools import tests as tests_tool


def _make_app(
    tmp_path: Path,
    *,
    test_command: str | None = "pytest -q",
) -> GhiaApp:
    cfg = Config(
        token="ghp_" + "x" * 36,
        repo="octo/hello",
        label="ai-fix",
        mode="semi",
        poll_interval_min=30,
        test_command=test_command,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    return GhiaApp(
        config=cfg,
        session=SessionStore(tmp_path / "session.json"),
        repo_root=repo,
        logger=logging.getLogger("ghia-test-runtests"),
    )


# ----------------------------------------------------------------------
# Skip / config gates
# ----------------------------------------------------------------------


async def test_run_tests_skipped_when_no_command(tmp_path: Path) -> None:
    app = _make_app(tmp_path, test_command=None)
    resp = await tests_tool.run_tests(app)
    assert resp.success
    assert resp.data["skipped"] is True


async def test_run_tests_disallowed_command_rejected(tmp_path: Path) -> None:
    """Defense-in-depth: bypass config validation, runtime still rejects."""

    app = _make_app(tmp_path, test_command="pytest")
    # Mutate past validation — simulates a corrupted in-memory cfg.
    app.config.test_command = "rm -rf /"
    resp = await tests_tool.run_tests(app)
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT


# ----------------------------------------------------------------------
# Docker availability
# ----------------------------------------------------------------------


async def test_run_tests_returns_docker_unavailable_when_no_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, test_command="pytest -q")
    monkeypatch.setattr(tests_tool, "docker_available", lambda: False)

    resp = await tests_tool.run_tests(app)
    assert not resp.success
    assert resp.code == ErrorCode.DOCKER_UNAVAILABLE


# ----------------------------------------------------------------------
# Happy / failure paths
# ----------------------------------------------------------------------


def _patch_runner(
    monkeypatch: pytest.MonkeyPatch, *, result: dict[str, Any] | None = None,
    raises: Exception | None = None,
) -> dict[str, Any]:
    """Replace ``DockerRunner.run_command`` with a controlled fake.

    Returns a dict that captures the call's kwargs so tests can
    assert on what was sent down to the runner.
    """

    captured: dict[str, Any] = {}

    async def _fake_run_command(self: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        if raises is not None:
            raise raises
        return result or {
            "exit_code": 0,
            "output": "ok",
            "timed_out": False,
            "duration_sec": 0.5,
        }

    monkeypatch.setattr(
        tests_tool.DockerRunner, "run_command", _fake_run_command
    )
    monkeypatch.setattr(tests_tool, "docker_available", lambda: True)
    return captured


async def test_run_tests_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, test_command="pytest -q")
    captured = _patch_runner(
        monkeypatch,
        result={"exit_code": 0, "output": "ok\n", "timed_out": False, "duration_sec": 1.2},
    )

    resp = await tests_tool.run_tests(app)
    assert resp.success, resp.error
    assert resp.data["passed"] is True
    assert resp.data["exit_code"] == 0
    assert resp.data["timed_out"] is False
    assert resp.data["duration_sec"] == 1.2
    # The runner must have been called with sh -c <test_command>.
    assert captured["command"] == ["sh", "-c", "pytest -q"]


async def test_run_tests_failure_returns_passed_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, test_command="pytest")
    _patch_runner(
        monkeypatch,
        result={"exit_code": 1, "output": "fail!", "timed_out": False, "duration_sec": 0.7},
    )

    resp = await tests_tool.run_tests(app)
    # Test failure is success-with-passed=False, NOT TEST_FAILED.
    assert resp.success
    assert resp.data["passed"] is False
    assert resp.data["exit_code"] == 1


async def test_run_tests_timeout_surfaces_timed_out_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, test_command="pytest")
    _patch_runner(
        monkeypatch,
        result={"exit_code": -1, "output": "", "timed_out": True, "duration_sec": 600.0},
    )

    resp = await tests_tool.run_tests(app)
    assert resp.success
    assert resp.data["passed"] is False
    assert resp.data["timed_out"] is True


async def test_run_tests_uses_ten_minute_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, test_command="pytest")
    captured = _patch_runner(monkeypatch)

    await tests_tool.run_tests(app)
    assert captured["timeout_sec"] == 600


async def test_run_tests_test_failed_on_mid_run_docker_death(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, test_command="pytest")
    _patch_runner(monkeypatch, raises=DockerUnavailable("daemon died"))

    resp = await tests_tool.run_tests(app)
    assert not resp.success
    assert resp.code == ErrorCode.TEST_FAILED


async def test_run_tests_uses_repo_root_as_mount_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, test_command="pytest")
    captured = _patch_runner(monkeypatch)

    await tests_tool.run_tests(app)
    assert captured["repo_path"] == app.repo_root
