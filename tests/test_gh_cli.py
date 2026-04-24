"""GH CLI integration tests (v0.2 refactor).

We mock at the subprocess boundary — every public coro in
``ghia.integrations.gh_cli`` funnels through ``_run_gh_sync``, so a
single ``monkeypatch.setattr(gh_cli, "_run_gh_sync", ...)`` covers
the entire surface without having to script async behaviour.

Coverage:
* ``repo_view`` 404 → REPO_NOT_FOUND
* ``list_issues`` happy path returns canonical-shape dicts
* ``auth_status`` parses single- and multi-account text correctly
* ``create_pull_request`` "already exists" → PR_EXISTS
* Auth-required (401) → TOKEN_INVALID
* Rate-limit → RATE_LIMITED with parsed reset_at when present
* gh missing → GhUnavailable from the public coros that need gh
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

from ghia.errors import ErrorCode
from ghia.integrations import gh_cli
from ghia.integrations.gh_cli import GhAuthError, GhUnavailable


def _completed(
    *,
    rc: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    """Build a CompletedProcess with the given outcome."""

    return subprocess.CompletedProcess(
        args=["gh"], returncode=rc, stdout=stdout, stderr=stderr
    )


def _patch_run_gh(
    monkeypatch: pytest.MonkeyPatch,
    proc: subprocess.CompletedProcess[str] | None = None,
    *,
    raises: Exception | None = None,
) -> list[list[str]]:
    """Mock ``_run_gh_sync`` and capture argv lists.

    Returns the list that gets appended to on every call so tests can
    assert exact argv shapes.
    """

    captured_argvs: list[list[str]] = []

    def fake(argv: list[str], *, input_text: Optional[str] = None) -> Any:
        captured_argvs.append(list(argv))
        if raises is not None:
            raise raises
        return proc if proc is not None else _completed()

    monkeypatch.setattr(gh_cli, "_run_gh_sync", fake)
    return captured_argvs


# ----------------------------------------------------------------------
# gh_available — pure shutil.which wrapper
# ----------------------------------------------------------------------


def test_gh_available_returns_true_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gh_cli.shutil, "which", lambda _name: "/usr/bin/gh")
    assert gh_cli.gh_available() is True


def test_gh_available_returns_false_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gh_cli.shutil, "which", lambda _name: None)
    assert gh_cli.gh_available() is False


# ----------------------------------------------------------------------
# repo_view
# ----------------------------------------------------------------------


async def test_repo_view_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "name": "hello",
        "nameWithOwner": "octo/hello",
        "viewerPermission": "ADMIN",
        "defaultBranchRef": {"name": "main"},
    }
    _patch_run_gh(monkeypatch, _completed(stdout=json.dumps(payload)))

    result = await gh_cli.repo_view("octo/hello")
    assert result["name"] == "hello"
    assert result["viewerPermission"] == "ADMIN"


async def test_repo_view_404_maps_to_repo_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run_gh(
        monkeypatch,
        _completed(
            rc=1,
            stderr="GraphQL: Could not resolve to a Repository with the name 'octo/missing'.",
        ),
    )

    with pytest.raises(GhAuthError) as info:
        await gh_cli.repo_view("octo/missing")
    assert info.value.code == ErrorCode.REPO_NOT_FOUND


# ----------------------------------------------------------------------
# list_issues
# ----------------------------------------------------------------------


async def test_list_issues_happy_path_returns_canonical_dicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Field renames (camelCase → snake_case) happen at this boundary."""

    raw = [
        {
            "number": 1,
            "title": "fix it",
            "body": "the body",
            "labels": [{"name": "bug"}, {"name": "ai-fix"}],
            "url": "https://github.com/octo/hello/issues/1",
            "createdAt": "2026-04-01T00:00:00Z",
            "updatedAt": "2026-04-02T00:00:00Z",
            "author": {"login": "alice"},
            "assignees": [{"login": "bob"}],
            "comments": [{"id": 1}, {"id": 2}],  # gh returns the list, not a count
        }
    ]
    argvs = _patch_run_gh(monkeypatch, _completed(stdout=json.dumps(raw)))

    issues = await gh_cli.list_issues("octo/hello", label="ai-fix")

    # Field shapes match the historical contract.
    assert issues == [
        {
            "number": 1,
            "title": "fix it",
            "body": "the body",
            "labels": ["bug", "ai-fix"],
            "html_url": "https://github.com/octo/hello/issues/1",
            "created_at": "2026-04-01T00:00:00Z",
            "updated_at": "2026-04-02T00:00:00Z",
            "author": "alice",
            "assignees": ["bob"],
            "comments_count": 2,
        }
    ]
    # Argv shape includes --label and --repo correctly.
    argv = argvs[0]
    assert argv[:3] == ["gh", "issue", "list"]
    assert "--repo" in argv and argv[argv.index("--repo") + 1] == "octo/hello"
    assert "--label" in argv and argv[argv.index("--label") + 1] == "ai-fix"


async def test_list_issues_401_maps_to_token_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run_gh(
        monkeypatch,
        _completed(
            rc=1,
            stderr="HTTP 401: Bad credentials (https://api.github.com/repos/...)",
        ),
    )

    with pytest.raises(GhAuthError) as info:
        await gh_cli.list_issues("octo/hello")
    assert info.value.code == ErrorCode.TOKEN_INVALID


async def test_list_issues_rate_limit_maps_to_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run_gh(
        monkeypatch,
        _completed(
            rc=1,
            stderr="API rate limit exceeded for user ID 12345.",
        ),
    )

    with pytest.raises(GhAuthError) as info:
        await gh_cli.list_issues("octo/hello")
    assert info.value.code == ErrorCode.RATE_LIMITED
    # Without an X-RateLimit-Reset header, reset_at is None and the
    # message uses the pinned "reset time unavailable" suffix.
    assert "reset time unavailable" in info.value.message


async def test_list_issues_rate_limit_with_reset_header_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When stderr includes an X-RateLimit-Reset epoch, we surface it."""

    # 2099 → far future so ``format_rate_limit_reset`` produces a "in <rel>"
    # string rather than "already reset".
    far_future_epoch = int(datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp())
    _patch_run_gh(
        monkeypatch,
        _completed(
            rc=1,
            stderr=(
                f"API rate limit exceeded for user.\n"
                f"X-RateLimit-Reset: {far_future_epoch}\n"
            ),
        ),
    )

    with pytest.raises(GhAuthError) as info:
        await gh_cli.list_issues("octo/hello")
    assert info.value.code == ErrorCode.RATE_LIMITED
    assert info.value.reset_at is not None
    assert "resets at" in info.value.message


# ----------------------------------------------------------------------
# auth_status
# ----------------------------------------------------------------------


async def test_auth_status_unauthenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gh exits 1 when no account is logged in."""

    _patch_run_gh(
        monkeypatch,
        _completed(rc=1, stderr="You are not logged into any GitHub hosts."),
    )
    status = await gh_cli.auth_status()
    assert status["authenticated"] is False
    assert status["active_account"] is None


async def test_auth_status_single_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = (
        "github.com\n"
        "  ✓ Logged in to github.com account octocat (keyring)\n"
        "  - Active account: true\n"
        "  - Git operations protocol: ssh\n"
    )
    _patch_run_gh(monkeypatch, _completed(stdout=text))

    status = await gh_cli.auth_status()
    assert status["authenticated"] is True
    assert status["active_account"] == "octocat"


async def test_auth_status_multi_account_picks_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When multiple accounts are configured, the Active one is returned."""

    text = (
        "github.com\n"
        "  ✓ Logged in to github.com account oldaccount (keyring)\n"
        "  - Active account: false\n"
        "  ✓ Logged in to github.com account currentuser (keyring)\n"
        "  - Active account: true\n"
        "  - Git operations protocol: https\n"
    )
    _patch_run_gh(monkeypatch, _completed(stdout=text))

    status = await gh_cli.auth_status()
    assert status["authenticated"] is True
    assert status["active_account"] == "currentuser"


async def test_auth_status_logged_in_without_active_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Older gh outputs may omit the Active marker; pick the first login."""

    text = (
        "github.com\n"
        "  ✓ Logged in to github.com account legacyuser (oauth_token)\n"
    )
    _patch_run_gh(monkeypatch, _completed(stdout=text))

    status = await gh_cli.auth_status()
    assert status["authenticated"] is True
    assert status["active_account"] == "legacyuser"


# ----------------------------------------------------------------------
# create_pull_request
# ----------------------------------------------------------------------


async def test_create_pull_request_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run_gh(
        monkeypatch,
        _completed(stdout="https://github.com/octo/hello/pull/42\n"),
    )

    result = await gh_cli.create_pull_request(
        "octo/hello",
        title="Fix #1",
        body="Closes #1",
        base="main",
        head="feature/x",
        draft=False,
    )
    assert result["number"] == 42
    assert result["html_url"] == "https://github.com/octo/hello/pull/42"
    assert result["draft"] is False


async def test_create_pull_request_draft_passes_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argvs = _patch_run_gh(
        monkeypatch,
        _completed(stdout="https://github.com/octo/hello/pull/7\n"),
    )

    await gh_cli.create_pull_request(
        "octo/hello",
        title="t",
        body="b",
        base="main",
        head="feature/y",
        draft=True,
    )
    assert "--draft" in argvs[0]


async def test_create_pull_request_already_exists_maps_to_pr_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run_gh(
        monkeypatch,
        _completed(
            rc=1,
            stderr=(
                'a pull request for branch "feature/dup" into branch '
                '"main" already exists\n'
            ),
        ),
    )

    with pytest.raises(GhAuthError) as info:
        await gh_cli.create_pull_request(
            "octo/hello",
            title="t",
            body="b",
            base="main",
            head="feature/dup",
            draft=False,
        )
    assert info.value.code == ErrorCode.PR_EXISTS


# ----------------------------------------------------------------------
# Network failure classification
# ----------------------------------------------------------------------


async def test_network_failure_maps_to_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run_gh(
        monkeypatch,
        _completed(
            rc=1,
            stderr="dial tcp: lookup api.github.com: no such host",
        ),
    )

    with pytest.raises(GhAuthError) as info:
        await gh_cli.list_issues("octo/hello")
    assert info.value.code == ErrorCode.NETWORK_ERROR


# ----------------------------------------------------------------------
# gh missing → GhUnavailable
# ----------------------------------------------------------------------


def test_run_gh_sync_raises_gh_unavailable_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gh_cli, "gh_available", lambda: False)
    with pytest.raises(GhUnavailable):
        gh_cli._run_gh_sync(["gh", "repo", "view"])


# ----------------------------------------------------------------------
# get_issue, post_issue_comment, add_label, list_open_prs smoke
# ----------------------------------------------------------------------


async def test_get_issue_returns_normalized_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = {
        "number": 5,
        "title": "T",
        "body": "B",
        "labels": [{"name": "bug"}],
        "url": "https://github.com/octo/hello/issues/5",
        "createdAt": "2026-04-01T00:00:00Z",
        "updatedAt": "2026-04-01T00:00:00Z",
        "author": {"login": "alice"},
        "assignees": [],
        "comments": [],
    }
    _patch_run_gh(monkeypatch, _completed(stdout=json.dumps(raw)))

    issue = await gh_cli.get_issue("octo/hello", 5)
    assert issue["number"] == 5
    assert issue["labels"] == ["bug"]
    assert issue["author"] == "alice"
    assert issue["comments_count"] == 0


async def test_post_issue_comment_returns_url_and_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run_gh(
        monkeypatch,
        _completed(stdout="https://github.com/octo/hello/issues/5#issuecomment-1\n"),
    )

    result = await gh_cli.post_issue_comment("octo/hello", 5, body="hi")
    assert result["html_url"] == (
        "https://github.com/octo/hello/issues/5#issuecomment-1"
    )
    # ``created_at`` is fabricated as "now" because ``gh issue comment``
    # has no JSON output mode.  Just make sure it's a parseable ISO8601.
    datetime.fromisoformat(result["created_at"].replace("Z", "+00:00"))


async def test_add_label_invokes_correct_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argvs = _patch_run_gh(monkeypatch)

    await gh_cli.add_label("octo/hello", 7, "human-review")

    argv = argvs[0]
    assert argv[:3] == ["gh", "issue", "edit"]
    assert "7" in argv
    assert "--add-label" in argv
    assert argv[argv.index("--add-label") + 1] == "human-review"


async def test_list_open_prs_normalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = [
        {
            "number": 1,
            "title": "T",
            "body": "B",
            "url": "https://github.com/octo/hello/pull/1",
            "headRefName": "feature/x",
        }
    ]
    _patch_run_gh(monkeypatch, _completed(stdout=json.dumps(raw)))

    prs = await gh_cli.list_open_prs("octo/hello")
    assert prs == [
        {
            "number": 1,
            "title": "T",
            "body": "B",
            "html_url": "https://github.com/octo/hello/pull/1",
            "head_ref": "feature/x",
        }
    ]
