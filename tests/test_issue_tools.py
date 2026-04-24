"""TRD-016-TEST + TRD-017-TEST — issue tools and duplicate detection.

Strategy: mock at the :class:`GitHubClient` boundary, not at the HTTP
layer.  TRD-015-TEST already covers the HTTP boundary; here we focus
on tool-layer behaviour (label filtering, priority derivation, queue
mutation, signal aggregation) where the client is just plumbing.

A tiny fake client implements only the methods each test exercises;
its instances are wired into ``GhiaApp`` by patching
:func:`ghia.tools.issues._get_client`.
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
from ghia.integrations.github import GitHubClientError
from ghia.tools import issues as issue_tools


_FAKE_TOKEN = "ghp_" + "y" * 36
_REPO = "octo/hello"


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    """Strip filters left by the client so tests don't cross-pollute."""

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
        "token": _FAKE_TOKEN,
        "repo": _REPO,
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
    return await create_app(repo_root=repo_root, config_path=cfg_path)


# ----------------------------------------------------------------------
# Fake client
# ----------------------------------------------------------------------


class _FakeClient:
    """Stand-in for :class:`GitHubClient` with method-level scripting."""

    def __init__(self) -> None:
        self.issues_by_label: dict[Optional[str], list[dict[str, Any]]] = {}
        self.issues_by_number: dict[int, dict[str, Any]] = {}
        self.posted_comments: list[tuple[int, str]] = []
        self.comment_response: dict[str, Any] = {
            "id": 1,
            "html_url": "https://example/comment",
            "created_at": "2026-04-01T00:00:00Z",
        }
        self.open_prs: list[dict[str, Any]] = []
        self.raise_on_list: Optional[GitHubClientError] = None
        self.raise_on_post: Optional[GitHubClientError] = None
        self.raise_on_pulls: Optional[GitHubClientError] = None
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def list_issues(
        self, label: Optional[str] = None, state: str = "open"
    ) -> list[dict[str, Any]]:
        self.calls.append(("list_issues", (), {"label": label, "state": state}))
        if self.raise_on_list:
            raise self.raise_on_list
        # ``None`` key collects "all" issues.
        return list(self.issues_by_label.get(label, []))

    async def get_issue(self, number: int) -> dict[str, Any]:
        self.calls.append(("get_issue", (), {"number": number}))
        return self.issues_by_number[number]

    async def post_issue_comment(
        self, number: int, body: str
    ) -> dict[str, Any]:
        self.calls.append(
            ("post_issue_comment", (), {"number": number, "body": body})
        )
        if self.raise_on_post:
            raise self.raise_on_post
        self.posted_comments.append((number, body))
        return dict(self.comment_response)

    async def list_open_prs(self) -> list[dict[str, Any]]:
        self.calls.append(("list_open_prs", (), {}))
        if self.raise_on_pulls:
            raise self.raise_on_pulls
        return list(self.open_prs)


@pytest.fixture
def fake_client(
    monkeypatch: pytest.MonkeyPatch,
) -> _FakeClient:
    """Inject a :class:`_FakeClient` for any ``_get_client`` call."""

    fake = _FakeClient()
    monkeypatch.setattr(issue_tools, "_get_client", lambda app: fake)
    return fake


# ----------------------------------------------------------------------
# Issue dict builder
# ----------------------------------------------------------------------


def _issue(
    number: int,
    *,
    title: str = "T",
    labels: Optional[list[str]] = None,
    body: str = "B",
) -> dict[str, Any]:
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


# ----------------------------------------------------------------------
# list_issues — TRD-016 AC
# ----------------------------------------------------------------------


async def test_list_issues_uses_configured_label_by_default(
    app: GhiaApp, fake_client: _FakeClient
) -> None:
    fake_client.issues_by_label["ai-fix"] = [_issue(1, labels=["ai-fix"])]

    resp = await issue_tools.list_issues(app)

    assert resp.success
    assert resp.data["count"] == 1
    # Verify the configured label was actually passed to the client.
    last = fake_client.calls[-1]
    assert last[2]["label"] == "ai-fix"


async def test_list_issues_explicit_label_overrides_default(
    app: GhiaApp, fake_client: _FakeClient
) -> None:
    fake_client.issues_by_label["bug"] = [_issue(7, labels=["bug"])]
    fake_client.issues_by_label["ai-fix"] = [_issue(1, labels=["ai-fix"])]

    resp = await issue_tools.list_issues(app, label="bug")

    assert resp.success
    assert resp.data["count"] == 1
    assert resp.data["issues"][0]["number"] == 7


async def test_list_issues_empty_label_means_all(
    app: GhiaApp, fake_client: _FakeClient
) -> None:
    fake_client.issues_by_label[None] = [_issue(1), _issue(2)]

    resp = await issue_tools.list_issues(app, label="")

    assert resp.success
    assert resp.data["count"] == 2


async def test_list_issues_priority_derivation(
    app: GhiaApp, fake_client: _FakeClient
) -> None:
    """Priority is derived from labels even when label filter is broad."""

    fake_client.issues_by_label["ai-fix"] = [
        _issue(1, labels=["bug"]),
        _issue(2, labels=["enhancement"]),
        _issue(3, labels=["documentation"]),
        _issue(4, labels=["ai-fix"]),  # no priority signal -> normal
        _issue(5, labels=["priority/high", "documentation"]),
    ]

    resp = await issue_tools.list_issues(app)
    by_num = {i["number"]: i for i in resp.data["issues"]}

    assert by_num[1]["priority"] == "high"
    assert by_num[2]["priority"] == "normal"
    assert by_num[3]["priority"] == "low"
    assert by_num[4]["priority"] == "normal"
    # High wins over docs.
    assert by_num[5]["priority"] == "high"


async def test_list_issues_propagates_client_error_as_structured_response(
    app: GhiaApp, fake_client: _FakeClient
) -> None:
    fake_client.raise_on_list = GitHubClientError(
        ErrorCode.RATE_LIMITED, "quota gone"
    )

    resp = await issue_tools.list_issues(app)

    assert not resp.success
    assert resp.code == ErrorCode.RATE_LIMITED
    assert "quota gone" in (resp.error or "")


# ----------------------------------------------------------------------
# get_issue
# ----------------------------------------------------------------------


async def test_get_issue_returns_annotated_dict(
    app: GhiaApp, fake_client: _FakeClient
) -> None:
    fake_client.issues_by_number[42] = _issue(42, labels=["bug"])

    resp = await issue_tools.get_issue(app, 42)

    assert resp.success
    assert resp.data["number"] == 42
    assert resp.data["priority"] == "high"


# ----------------------------------------------------------------------
# pick_issue / skip_issue — queue mutation
# ----------------------------------------------------------------------


async def test_pick_issue_appends_to_queue(
    app: GhiaApp, fake_client: _FakeClient
) -> None:
    resp = await issue_tools.pick_issue(app, 7)
    assert resp.success
    assert resp.data["queue"] == [7]
    state = await app.session.read()
    assert state.queue == [7]


async def test_pick_issue_refuses_duplicate_silently(
    app: GhiaApp, fake_client: _FakeClient
) -> None:
    """Re-picking the same number is idempotent — no error, no growth."""

    await issue_tools.pick_issue(app, 7)
    resp = await issue_tools.pick_issue(app, 7)
    assert resp.success
    assert resp.data["queue"] == [7]


async def test_pick_issue_preserves_order(
    app: GhiaApp, fake_client: _FakeClient
) -> None:
    for n in [3, 1, 4, 1, 5]:
        await issue_tools.pick_issue(app, n)
    state = await app.session.read()
    assert state.queue == [3, 1, 4, 5]


async def test_skip_issue_removes_from_queue_and_records_skip(
    app: GhiaApp, fake_client: _FakeClient
) -> None:
    await issue_tools.pick_issue(app, 11)
    await issue_tools.pick_issue(app, 12)

    resp = await issue_tools.skip_issue(app, 11)

    assert resp.success
    assert resp.data["queue"] == [12]
    assert resp.data["skipped"] == [11]
    state = await app.session.read()
    assert state.queue == [12]
    assert state.skipped == [11]


async def test_skip_issue_not_in_queue_still_recorded(
    app: GhiaApp, fake_client: _FakeClient
) -> None:
    """Skipping an issue we never picked is allowed."""

    resp = await issue_tools.skip_issue(app, 99)
    assert resp.success
    assert 99 in resp.data["skipped"]


async def test_skip_issue_dedups_skipped_list(
    app: GhiaApp, fake_client: _FakeClient
) -> None:
    await issue_tools.skip_issue(app, 5)
    await issue_tools.skip_issue(app, 5)
    state = await app.session.read()
    assert state.skipped == [5]


# ----------------------------------------------------------------------
# post_issue_comment — TRD-016 AC
# ----------------------------------------------------------------------


async def test_post_issue_comment_passes_body_through(
    app: GhiaApp, fake_client: _FakeClient
) -> None:
    body = "Working on this — branch fix-issue-7 created."
    resp = await issue_tools.post_issue_comment(app, 7, body)

    assert resp.success
    assert fake_client.posted_comments == [(7, body)]
    # Returned dict mirrors the client's response.
    assert resp.data["id"] == fake_client.comment_response["id"]


async def test_post_issue_comment_rejects_empty_body(
    app: GhiaApp, fake_client: _FakeClient
) -> None:
    resp = await issue_tools.post_issue_comment(app, 7, "   ")
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT
    assert fake_client.posted_comments == []


async def test_post_issue_comment_propagates_client_error(
    app: GhiaApp, fake_client: _FakeClient
) -> None:
    fake_client.raise_on_post = GitHubClientError(
        ErrorCode.TOKEN_INVALID, "bad creds"
    )
    resp = await issue_tools.post_issue_comment(app, 7, "hi")
    assert not resp.success
    assert resp.code == ErrorCode.TOKEN_INVALID


# ----------------------------------------------------------------------
# check_issue_has_open_pr — TRD-017 AC
# ----------------------------------------------------------------------


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


async def test_check_issue_no_signals_returns_clean(
    app: GhiaApp,
    fake_client: _FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client.open_prs = [_pr(1, title="unrelated", body="nothing here")]

    # Force "no branches" to make this test independent of repo state.
    def _no_branches(*a: Any, **kw: Any) -> Any:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _no_branches)

    resp = await issue_tools.check_issue_has_open_pr(app, 42)
    assert resp.success
    assert resp.data["has_duplicate"] is False
    assert resp.data["signals"] == []


async def test_check_issue_pr_signal_only(
    app: GhiaApp,
    fake_client: _FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client.open_prs = [
        _pr(1, title="unrelated", body=""),
        _pr(2, title="Closes #42 — fix the thing", body=""),
    ]

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
    app: GhiaApp,
    fake_client: _FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client.open_prs = []

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
    app: GhiaApp,
    fake_client: _FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both PR and branch signals present — both must appear."""

    fake_client.open_prs = [
        _pr(99, title="Fixes #7", body="closing it", head_ref="fix-issue-7"),
    ]

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
    app: GhiaApp,
    fake_client: _FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``#12`` must not match a PR that mentions ``#123``."""

    fake_client.open_prs = [_pr(1, title="Closes #123", body="")]

    def _no_branches(*a: Any, **kw: Any) -> Any:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _no_branches)

    resp = await issue_tools.check_issue_has_open_pr(app, 12)
    assert resp.success
    assert resp.data["has_duplicate"] is False


async def test_check_issue_subprocess_failure_treated_as_no_branch(
    app: GhiaApp,
    fake_client: _FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing git binary must NOT cause the tool to fail."""

    fake_client.open_prs = []

    def _git_missing(*a: Any, **kw: Any) -> Any:
        raise FileNotFoundError("git not on PATH")

    monkeypatch.setattr(subprocess, "run", _git_missing)

    resp = await issue_tools.check_issue_has_open_pr(app, 42)
    assert resp.success
    assert resp.data["has_duplicate"] is False
    assert resp.data["signals"] == []


async def test_check_issue_subprocess_nonzero_treated_as_no_branch(
    app: GhiaApp,
    fake_client: _FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``git`` returning non-zero (not a repo, etc.) is also benign."""

    fake_client.open_prs = []

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
    app: GhiaApp,
    fake_client: _FakeClient,
) -> None:
    """A PR-listing failure must surface as a structured error response."""

    fake_client.raise_on_pulls = GitHubClientError(
        ErrorCode.RATE_LIMITED, "out of quota"
    )
    resp = await issue_tools.check_issue_has_open_pr(app, 7)
    assert not resp.success
    assert resp.code == ErrorCode.RATE_LIMITED


# ----------------------------------------------------------------------
# Lazy client cache
# ----------------------------------------------------------------------


async def test_get_client_caches_per_token_and_repo(
    app: GhiaApp,
) -> None:
    """Same (token, repo) -> same instance; change either -> new instance."""

    a = issue_tools._get_client(app)
    b = issue_tools._get_client(app)
    assert a is b

    # Mutate the repo on the config and request again.
    app.config.repo = "octo/other"
    c = issue_tools._get_client(app)
    assert c is not a
