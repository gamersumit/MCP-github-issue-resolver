"""TRD-016-TEST + TRD-017-TEST — issue tools and duplicate detection.

v0.2 strategy: mock at the :mod:`ghia.integrations.gh_cli` boundary.
The gh-cli module's own tests cover subprocess plumbing; here we
focus on tool-layer behaviour (label filtering, priority derivation,
queue mutation, signal aggregation) where gh_cli is just plumbing.

Each test patches the specific gh_cli function it cares about with a
lightweight async stub.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Optional

import pytest

from ghia import redaction
from ghia.app import GhiaApp, create_app
from ghia.errors import ErrorCode
from ghia.integrations import gh_cli
from ghia.integrations.gh_cli import GhAuthError
from ghia.tools import issues as issue_tools


_REPO = "octo/hello"


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    """Strip filters left by other modules so tests don't cross-pollute."""

    root = logging.getLogger()
    before = list(root.filters)
    redaction.set_token(None)
    yield
    for f in list(root.filters):
        if f not in before:
            root.removeFilter(f)
    redaction.set_token(None)


def _write_config(path: Path, **overrides: Any) -> None:
    payload: dict[str, Any] = {
        "label": "ai-fix",
        "mode": "semi",
        "poll_interval_min": 30,
    }
    payload.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


@pytest.fixture
async def app(tmp_path: Path) -> GhiaApp:
    cfg_path = tmp_path / "cfg.json"
    _write_config(cfg_path)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    return await create_app(
        repo_root=repo_root, config_path=cfg_path, repo_full_name=_REPO
    )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _issue(
    number: int,
    *,
    title: str = "T",
    labels: Optional[list[str]] = None,
    body: str = "B",
) -> dict[str, Any]:
    """Build an issue dict in the canonical (post-normalization) shape."""

    return {
        "number": number,
        "title": title,
        "body": body,
        "labels": labels or [],
        "html_url": f"https://github.com/octo/hello/issues/{number}",
        "created_at": "2026-04-01T00:00:00Z",
        "updated_at": "2026-04-01T00:00:00Z",
        "author": "alice",
        "assignees": [],
        "comments_count": 0,
    }


def _pr(
    number: int,
    *,
    title: str = "PR",
    body: str = "",
    head_ref: str = "feat",
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "body": body,
        "html_url": f"https://github.com/octo/hello/pull/{number}",
        "head_ref": head_ref,
    }


# ----------------------------------------------------------------------
# list_issues — TRD-016 AC
# ----------------------------------------------------------------------


async def test_list_issues_uses_configured_label_by_default(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_list_issues(repo: str, **kwargs: Any) -> list[dict[str, Any]]:
        captured["repo"] = repo
        captured["kwargs"] = kwargs
        return [_issue(1, labels=["ai-fix"])]

    monkeypatch.setattr(gh_cli, "list_issues", fake_list_issues)

    resp = await issue_tools.list_issues(app)

    assert resp.success
    assert resp.data["count"] == 1
    assert captured["repo"] == _REPO
    assert captured["kwargs"]["label"] == "ai-fix"


async def test_list_issues_explicit_label_overrides_default(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_list_issues(repo: str, **kwargs: Any) -> list[dict[str, Any]]:
        captured["kwargs"] = kwargs
        return [_issue(7, labels=["bug"])]

    monkeypatch.setattr(gh_cli, "list_issues", fake_list_issues)

    resp = await issue_tools.list_issues(app, label="bug")

    assert resp.success
    assert captured["kwargs"]["label"] == "bug"
    assert resp.data["issues"][0]["number"] == 7


async def test_list_issues_empty_label_means_all(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_list_issues(repo: str, **kwargs: Any) -> list[dict[str, Any]]:
        captured["kwargs"] = kwargs
        return [_issue(1), _issue(2)]

    monkeypatch.setattr(gh_cli, "list_issues", fake_list_issues)

    resp = await issue_tools.list_issues(app, label="")

    assert resp.success
    assert captured["kwargs"]["label"] is None
    assert resp.data["count"] == 2


async def test_list_issues_priority_derivation(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Priority is derived from labels even when label filter is broad."""

    issues = [
        _issue(1, labels=["bug"]),
        _issue(2, labels=["enhancement"]),
        _issue(3, labels=["documentation"]),
        _issue(4, labels=["ai-fix"]),  # no priority signal -> normal
        _issue(5, labels=["priority/high", "documentation"]),
    ]

    async def fake_list_issues(repo: str, **kwargs: Any) -> list[dict[str, Any]]:
        return issues

    monkeypatch.setattr(gh_cli, "list_issues", fake_list_issues)

    resp = await issue_tools.list_issues(app)
    by_num = {i["number"]: i for i in resp.data["issues"]}

    assert by_num[1]["priority"] == "high"
    assert by_num[2]["priority"] == "normal"
    assert by_num[3]["priority"] == "low"
    assert by_num[4]["priority"] == "normal"
    # High wins over docs.
    assert by_num[5]["priority"] == "high"


async def test_list_issues_propagates_client_error_as_structured_response(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(repo: str, **kwargs: Any) -> Any:
        raise GhAuthError(ErrorCode.RATE_LIMITED, "quota gone")

    monkeypatch.setattr(gh_cli, "list_issues", boom)

    resp = await issue_tools.list_issues(app)

    assert not resp.success
    assert resp.code == ErrorCode.RATE_LIMITED
    assert "quota gone" in (resp.error or "")


# ----------------------------------------------------------------------
# get_issue
# ----------------------------------------------------------------------


async def test_get_issue_returns_annotated_dict(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get_issue(repo: str, *, number: int) -> dict[str, Any]:
        return _issue(number, labels=["bug"])

    monkeypatch.setattr(gh_cli, "get_issue", fake_get_issue)

    resp = await issue_tools.get_issue(app, 42)

    assert resp.success
    assert resp.data["number"] == 42
    assert resp.data["priority"] == "high"


# ----------------------------------------------------------------------
# pick_issue / skip_issue — queue mutation (no gh_cli involvement)
# ----------------------------------------------------------------------


async def test_pick_issue_appends_to_queue(app: GhiaApp) -> None:
    resp = await issue_tools.pick_issue(app, 7)
    assert resp.success
    assert resp.data["queue"] == [7]
    state = await app.session.read()
    assert state.queue == [7]


async def test_pick_issue_refuses_duplicate_silently(app: GhiaApp) -> None:
    """Re-picking the same number is idempotent — no error, no growth."""

    await issue_tools.pick_issue(app, 7)
    resp = await issue_tools.pick_issue(app, 7)
    assert resp.success
    assert resp.data["queue"] == [7]


async def test_pick_issue_preserves_order(app: GhiaApp) -> None:
    for n in [3, 1, 4, 1, 5]:
        await issue_tools.pick_issue(app, n)
    state = await app.session.read()
    assert state.queue == [3, 1, 4, 5]


async def test_skip_issue_removes_from_queue_and_records_skip(app: GhiaApp) -> None:
    await issue_tools.pick_issue(app, 11)
    await issue_tools.pick_issue(app, 12)

    resp = await issue_tools.skip_issue(app, 11)

    assert resp.success
    assert resp.data["queue"] == [12]
    assert resp.data["skipped"] == [11]
    state = await app.session.read()
    assert state.queue == [12]
    assert state.skipped == [11]


async def test_skip_issue_not_in_queue_still_recorded(app: GhiaApp) -> None:
    """Skipping an issue we never picked is allowed."""

    resp = await issue_tools.skip_issue(app, 99)
    assert resp.success
    assert 99 in resp.data["skipped"]


async def test_skip_issue_dedups_skipped_list(app: GhiaApp) -> None:
    await issue_tools.skip_issue(app, 5)
    await issue_tools.skip_issue(app, 5)
    state = await app.session.read()
    assert state.skipped == [5]


# ----------------------------------------------------------------------
# post_issue_comment — TRD-016 AC
# ----------------------------------------------------------------------


async def test_post_issue_comment_passes_body_through(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_post(repo: str, *, number: int, body: str) -> dict[str, Any]:
        captured["repo"] = repo
        captured["number"] = number
        captured["body"] = body
        return {"html_url": "https://example/comment", "created_at": "2026-04-01T00:00:00Z"}

    monkeypatch.setattr(gh_cli, "post_issue_comment", fake_post)

    body = "Working on this — branch fix-issue-7 created."
    resp = await issue_tools.post_issue_comment(app, 7, body)

    assert resp.success
    assert captured["number"] == 7
    assert captured["body"] == body
    assert resp.data["html_url"] == "https://example/comment"


async def test_post_issue_comment_rejects_empty_body(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = {"hit": False}

    async def fake_post(*args: Any, **kwargs: Any) -> Any:
        called["hit"] = True
        return {}

    monkeypatch.setattr(gh_cli, "post_issue_comment", fake_post)

    resp = await issue_tools.post_issue_comment(app, 7, "   ")
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT
    assert called["hit"] is False, "gh_cli must not be invoked on rejected input"


async def test_post_issue_comment_propagates_client_error(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(repo: str, *, number: int, body: str) -> Any:
        raise GhAuthError(ErrorCode.TOKEN_INVALID, "bad creds")

    monkeypatch.setattr(gh_cli, "post_issue_comment", boom)

    resp = await issue_tools.post_issue_comment(app, 7, "hi")
    assert not resp.success
    assert resp.code == ErrorCode.TOKEN_INVALID


# ----------------------------------------------------------------------
# check_issue_has_open_pr — TRD-017 AC
# ----------------------------------------------------------------------


async def test_check_issue_no_signals_returns_clean(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_prs(repo: str) -> list[dict[str, Any]]:
        return [_pr(1, title="unrelated", body="nothing here")]

    monkeypatch.setattr(gh_cli, "list_open_prs", fake_prs)

    # Force "no branches" to make this test independent of repo state.
    def _no_branches(*a: Any, **kw: Any) -> Any:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _no_branches)

    resp = await issue_tools.check_issue_has_open_pr(app, 42)
    assert resp.success
    assert resp.data["has_duplicate"] is False
    assert resp.data["signals"] == []


async def test_check_issue_pr_signal_only(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_prs(repo: str) -> list[dict[str, Any]]:
        return [
            _pr(1, title="unrelated", body=""),
            _pr(2, title="Closes #42 — fix the thing", body=""),
        ]

    monkeypatch.setattr(gh_cli, "list_open_prs", fake_prs)

    def _no_branches(*a: Any, **kw: Any) -> Any:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _no_branches)

    resp = await issue_tools.check_issue_has_open_pr(app, 42)
    assert resp.success
    assert resp.data["has_duplicate"] is True
    pr_signals = [s for s in resp.data["signals"] if s["type"] == "pr"]
    assert len(pr_signals) == 1
    assert pr_signals[0]["pr_number"] == 2


async def test_check_issue_branch_signal_only(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_prs(repo: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(gh_cli, "list_open_prs", fake_prs)

    def _has_branches(*a: Any, **kw: Any) -> Any:
        return subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="  fix-issue-42\n* issue-42-typo\n", stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _has_branches)

    resp = await issue_tools.check_issue_has_open_pr(app, 42)
    assert resp.success
    assert resp.data["has_duplicate"] is True
    branch_signals = [s for s in resp.data["signals"] if s["type"] == "branch"]
    assert {s["name"] for s in branch_signals} == {
        "fix-issue-42", "issue-42-typo",
    }


async def test_check_issue_both_signals_reported(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both PR and branch signals present — both must appear."""

    async def fake_prs(repo: str) -> list[dict[str, Any]]:
        return [_pr(99, title="Fixes #7", body="closing it", head_ref="fix-issue-7")]

    monkeypatch.setattr(gh_cli, "list_open_prs", fake_prs)

    def _has_branches(*a: Any, **kw: Any) -> Any:
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="  fix-issue-7\n", stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _has_branches)

    resp = await issue_tools.check_issue_has_open_pr(app, 7)
    assert resp.success
    types_seen = {s["type"] for s in resp.data["signals"]}
    assert types_seen == {"pr", "branch"}
    assert resp.data["has_duplicate"] is True


async def test_check_issue_does_not_match_substring_numbers(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``#12`` must not match a PR that mentions ``#123``."""

    async def fake_prs(repo: str) -> list[dict[str, Any]]:
        return [_pr(1, title="Closes #123", body="")]

    monkeypatch.setattr(gh_cli, "list_open_prs", fake_prs)

    def _no_branches(*a: Any, **kw: Any) -> Any:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _no_branches)

    resp = await issue_tools.check_issue_has_open_pr(app, 12)
    assert resp.success
    assert resp.data["has_duplicate"] is False


async def test_check_issue_subprocess_failure_treated_as_no_branch(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing git binary must NOT cause the tool to fail."""

    async def fake_prs(repo: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(gh_cli, "list_open_prs", fake_prs)

    def _git_missing(*a: Any, **kw: Any) -> Any:
        raise FileNotFoundError("git not on PATH")

    monkeypatch.setattr(subprocess, "run", _git_missing)

    resp = await issue_tools.check_issue_has_open_pr(app, 42)
    assert resp.success
    assert resp.data["has_duplicate"] is False
    assert resp.data["signals"] == []


async def test_check_issue_subprocess_nonzero_treated_as_no_branch(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``git`` returning non-zero (not a repo, etc.) is also benign."""

    async def fake_prs(repo: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(gh_cli, "list_open_prs", fake_prs)

    def _git_nonzero(*a: Any, **kw: Any) -> Any:
        return subprocess.CompletedProcess(
            args=[], returncode=128,
            stdout="", stderr="not a git repository",
        )

    monkeypatch.setattr(subprocess, "run", _git_nonzero)

    resp = await issue_tools.check_issue_has_open_pr(app, 42)
    assert resp.success
    assert resp.data["has_duplicate"] is False


async def test_check_issue_propagates_pr_listing_error(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PR-listing failure must surface as a structured error response."""

    async def boom(repo: str) -> Any:
        raise GhAuthError(ErrorCode.RATE_LIMITED, "out of quota")

    monkeypatch.setattr(gh_cli, "list_open_prs", boom)

    resp = await issue_tools.check_issue_has_open_pr(app, 7)
    assert not resp.success
    assert resp.code == ErrorCode.RATE_LIMITED
