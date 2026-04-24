"""Lint-tool tests (TRD-025-TEST).

The lint tool shells out to two external programs (``git`` and the
configured linter).  We mock both so the tests are hermetic — no
ruff / eslint / git binary required on the test runner.

Coverage:
* skip when ``lint_command`` is None
* skip when no files changed
* happy path: linter invoked with the right argv
* linter failure surfaces a structured non-zero return without
  crashing
* linter binary missing → INVALID_INPUT
* defense-in-depth: a config that bypasses the allow-list at
  load time still fails the runtime re-validation
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ghia.app import GhiaApp
from ghia.config import Config
from ghia.errors import ErrorCode
from ghia.session import SessionStore
from ghia.tools import lint as lint_tool


def _make_app(
    tmp_path: Path,
    *,
    lint_command: str | None = None,
) -> GhiaApp:
    cfg = Config(
        label="ai-fix",
        mode="semi",
        poll_interval_min=30,
        lint_command=lint_command,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    return GhiaApp(
        config=cfg,
        session=SessionStore(tmp_path / "session.json"),
        repo_root=repo,
        repo_full_name="octo/hello",
        logger=logging.getLogger("ghia-test-lint"),
    )


# ----------------------------------------------------------------------
# Skip cases
# ----------------------------------------------------------------------


async def test_lint_skipped_when_no_command_configured(tmp_path: Path) -> None:
    app = _make_app(tmp_path, lint_command=None)

    resp = await lint_tool.check_linting(app)

    assert resp.success
    assert resp.data["skipped"] is True
    assert "no lint command" in resp.data["reason"].lower()


async def test_lint_skipped_when_command_blank(tmp_path: Path) -> None:
    app = _make_app(tmp_path, lint_command="   ")
    resp = await lint_tool.check_linting(app)
    assert resp.success
    assert resp.data["skipped"] is True


async def test_lint_skipped_when_no_files_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, lint_command="ruff check")

    async def _empty_diff(_app: GhiaApp, *_a: str, **_k: Any) -> tuple[int, str, str]:
        return 0, "", ""

    monkeypatch.setattr(lint_tool, "_run_git", _empty_diff)

    resp = await lint_tool.check_linting(app)
    assert resp.success
    assert resp.data["passed"] is True
    assert resp.data["linted"] == []
    assert resp.data["skipped_no_changes"] is True


# ----------------------------------------------------------------------
# Happy / failure paths
# ----------------------------------------------------------------------


async def test_lint_happy_path_invokes_linter_with_changed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, lint_command="ruff check --select=E")
    # Create a real file so the existence filter passes.
    (app.repo_root / "a.py").write_text("x = 1\n")
    (app.repo_root / "b.py").write_text("y = 2\n")

    async def _diff(_app: GhiaApp, *_a: str, **_k: Any) -> tuple[int, str, str]:
        return 0, "a.py\nb.py\n", ""

    monkeypatch.setattr(lint_tool, "_run_git", _diff)

    captured: dict[str, Any] = {}

    def _fake_subprocess_run(argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="all good\n", stderr="")

    monkeypatch.setattr(lint_tool.subprocess, "run", _fake_subprocess_run)

    resp = await lint_tool.check_linting(app)
    assert resp.success, resp.error
    assert resp.data["passed"] is True
    assert resp.data["returncode"] == 0
    assert resp.data["files"] == ["a.py", "b.py"]
    # argv must be: ruff check --select=E a.py b.py
    assert captured["argv"][0] == "ruff"
    assert captured["argv"][-2:] == ["a.py", "b.py"]
    # No shell invocation — argv must be a list, not a string.
    assert isinstance(captured["argv"], list)


async def test_lint_failure_returns_passed_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, lint_command="ruff check")
    (app.repo_root / "a.py").write_text("import os\n")

    async def _diff(_app: GhiaApp, *_a: str, **_k: Any) -> tuple[int, str, str]:
        return 0, "a.py\n", ""

    monkeypatch.setattr(lint_tool, "_run_git", _diff)

    def _fake_run(argv: list[str], **_k: Any) -> Any:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="lint err\n")

    monkeypatch.setattr(lint_tool.subprocess, "run", _fake_run)

    resp = await lint_tool.check_linting(app)
    # Non-zero rc is success-with-passed=False, not a tool failure.
    assert resp.success
    assert resp.data["passed"] is False
    assert resp.data["returncode"] == 1
    assert "lint err" in resp.data["stderr"]


async def test_lint_filters_out_deleted_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, lint_command="ruff check")
    (app.repo_root / "exists.py").write_text("x = 1\n")
    # ``deleted.py`` deliberately does NOT exist on disk.

    async def _diff(_app: GhiaApp, *_a: str, **_k: Any) -> tuple[int, str, str]:
        return 0, "exists.py\ndeleted.py\n", ""

    monkeypatch.setattr(lint_tool, "_run_git", _diff)

    captured: dict[str, Any] = {}

    def _fake_run(argv: list[str], **_k: Any) -> Any:
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(lint_tool.subprocess, "run", _fake_run)

    resp = await lint_tool.check_linting(app)
    assert resp.success
    assert resp.data["files"] == ["exists.py"]
    assert "deleted.py" not in captured["argv"]


# ----------------------------------------------------------------------
# Error cases
# ----------------------------------------------------------------------


async def test_lint_binary_missing_returns_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, lint_command="ruff check")
    (app.repo_root / "a.py").write_text("x\n")

    async def _diff(_app: GhiaApp, *_a: str, **_k: Any) -> tuple[int, str, str]:
        return 0, "a.py\n", ""

    monkeypatch.setattr(lint_tool, "_run_git", _diff)

    def _fake_run(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("ruff")

    monkeypatch.setattr(lint_tool.subprocess, "run", _fake_run)

    resp = await lint_tool.check_linting(app)
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT
    assert "ruff" in resp.error.lower()


async def test_lint_disallowed_command_rejected_at_runtime(
    tmp_path: Path,
) -> None:
    """Defense-in-depth: in-memory mutation past config validation."""

    app = _make_app(tmp_path, lint_command="ruff check")
    # Bypass Config validation by directly mutating the field —
    # simulates a corrupted in-memory config that should still be
    # rejected by the tool's re-validation.
    app.config.lint_command = "rm -rf /"

    resp = await lint_tool.check_linting(app)
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT


async def test_lint_git_diff_failure_returns_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, lint_command="ruff check")

    async def _bad_diff(_app: GhiaApp, *_a: str, **_k: Any) -> tuple[int, str, str]:
        return 128, "", "fatal: not a git repository\n"

    monkeypatch.setattr(lint_tool, "_run_git", _bad_diff)

    resp = await lint_tool.check_linting(app)
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT
