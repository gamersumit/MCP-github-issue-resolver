"""PR creation tests (TRD-024-TEST, v0.2 refactor).

We mock the ``gh_cli.create_pull_request`` coroutine directly — that
boundary is exactly what ``pr.create_pr`` now calls.  The git
default-branch / current-branch lookups still go through
``_run_git`` on the git module; we monkeypatch that so the tests
don't need a real git binary.

Coverage:
* mode='full' default → draft=True passed to gh_cli
* mode='semi' default → draft=False
* explicit draft flag wins over mode
* body without close-marker gets one appended (assert through captured args)
* body with existing Fixes #N preserved (no duplicate)
* refusal on default branch
* PR_EXISTS error surfaces correctly
* gh missing → GIT_ERROR
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from ghia.app import GhiaApp
from ghia.config import Config
from ghia.errors import ErrorCode
from ghia.integrations import gh_cli
from ghia.integrations.gh_cli import GhAuthError, GhUnavailable
from ghia.session import SessionStore
from ghia.tools import git as git_tools
from ghia.tools import pr as pr_tool


def _make_app(tmp_path: Path, *, mode: str = "semi") -> GhiaApp:
    cfg = Config(
        label="ai-fix",
        mode=mode,  # type: ignore[arg-type]
        poll_interval_min=30,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    return GhiaApp(
        config=cfg,
        session=SessionStore(tmp_path / "session.json"),
        repo_root=repo,
        repo_full_name="octo/hello",
        logger=logging.getLogger("ghia-test-pr"),
    )


def _patch_branches(
    monkeypatch: pytest.MonkeyPatch, *, default: str, current: str
) -> None:
    """Stub out git default/current branch lookups."""

    async def _run_git_stub(
        _app: GhiaApp, *args: str, **_k: Any
    ) -> tuple[int, str, str]:
        # Drive both the default-branch detection (symbolic-ref) and
        # current-branch (rev-parse --abbrev-ref HEAD) through the
        # same stub.  We don't differentiate by argv because the
        # tests only need the resolved values.
        if args[:1] == ("rev-parse",):
            return 0, current + "\n", ""
        if args[:1] == ("symbolic-ref",):
            return 0, f"refs/remotes/origin/{default}\n", ""
        return 0, "", ""

    monkeypatch.setattr(git_tools, "_run_git", _run_git_stub)


def _patch_gh_cli_create(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: dict[str, Any] | None = None,
    raises: Exception | None = None,
) -> dict[str, Any]:
    """Mock gh_cli.create_pull_request and capture call args."""

    captured: dict[str, Any] = {}

    async def fake_create(repo: str, **kwargs: Any) -> dict[str, Any]:
        captured["repo"] = repo
        captured["kwargs"] = kwargs
        if raises is not None:
            raise raises
        return result or {
            "number": 42,
            "html_url": "https://github.com/octo/hello/pull/42",
            "draft": kwargs.get("draft", False),
            "head": kwargs.get("head"),
            "base": kwargs.get("base"),
        }

    monkeypatch.setattr(gh_cli, "create_pull_request", fake_create)
    return captured


# ----------------------------------------------------------------------
# Draft / mode behaviour
# ----------------------------------------------------------------------


async def test_full_mode_defaults_to_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, mode="full")
    await app.session.update(mode="full")
    _patch_branches(monkeypatch, default="main", current="feature/x")
    captured = _patch_gh_cli_create(monkeypatch)

    resp = await pr_tool.create_pr(
        app, issue_number=1, title="t", body="b"
    )
    assert resp.success, resp.error
    assert resp.data["draft"] is True
    assert captured["kwargs"]["draft"] is True


async def test_semi_mode_defaults_to_non_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, mode="semi")
    await app.session.update(mode="semi")
    _patch_branches(monkeypatch, default="main", current="feature/x")
    captured = _patch_gh_cli_create(monkeypatch)

    resp = await pr_tool.create_pr(
        app, issue_number=1, title="t", body="b"
    )
    assert resp.success, resp.error
    assert resp.data["draft"] is False
    assert captured["kwargs"]["draft"] is False


async def test_explicit_draft_overrides_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, mode="full")
    await app.session.update(mode="full")
    _patch_branches(monkeypatch, default="main", current="feature/x")
    captured = _patch_gh_cli_create(monkeypatch)

    resp = await pr_tool.create_pr(
        app, issue_number=1, title="t", body="b", draft=False
    )
    assert resp.success, resp.error
    assert resp.data["draft"] is False
    assert captured["kwargs"]["draft"] is False


# ----------------------------------------------------------------------
# Body / Closes-marker handling
# ----------------------------------------------------------------------


async def test_body_appends_closes_marker_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    _patch_branches(monkeypatch, default="main", current="feature/x")
    captured = _patch_gh_cli_create(monkeypatch)

    await pr_tool.create_pr(
        app, issue_number=42, title="t", body="some description"
    )
    body_val = captured["kwargs"]["body"]
    assert "Closes #42" in body_val


async def test_body_preserves_existing_closes_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    _patch_branches(monkeypatch, default="main", current="feature/x")
    captured = _patch_gh_cli_create(monkeypatch)

    user_body = "see also Fixes #42 inline"
    await pr_tool.create_pr(
        app, issue_number=42, title="t", body=user_body
    )
    body_val = captured["kwargs"]["body"]
    # Original body preserved, no duplicate "Closes #42" appended.
    assert "Fixes #42" in body_val
    assert "Closes #42" not in body_val


async def test_body_marker_uses_correct_issue_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Fixes #99`` should NOT satisfy the marker for issue #42."""

    app = _make_app(tmp_path)
    _patch_branches(monkeypatch, default="main", current="feature/x")
    captured = _patch_gh_cli_create(monkeypatch)

    await pr_tool.create_pr(
        app, issue_number=42, title="t", body="Fixes #99"
    )
    body_val = captured["kwargs"]["body"]
    assert "Closes #42" in body_val
    assert "Fixes #99" in body_val


# ----------------------------------------------------------------------
# Refusal & error mapping
# ----------------------------------------------------------------------


async def test_refuses_on_default_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    # current == default → refuse.
    _patch_branches(monkeypatch, default="main", current="main")

    # gh_cli must NOT be invoked when we refuse — patch with a raise
    # to assert non-call.
    async def _must_not_call(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("gh_cli.create_pull_request must not be called")

    monkeypatch.setattr(gh_cli, "create_pull_request", _must_not_call)

    resp = await pr_tool.create_pr(
        app, issue_number=1, title="t", body="b"
    )
    assert not resp.success
    assert resp.code == ErrorCode.ON_DEFAULT_BRANCH_REFUSED


async def test_pr_exists_maps_correctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    _patch_branches(monkeypatch, default="main", current="feature/dup")
    _patch_gh_cli_create(
        monkeypatch,
        raises=GhAuthError(
            ErrorCode.PR_EXISTS,
            'a pull request for branch "feature/dup" into branch "main" already exists',
        ),
    )

    resp = await pr_tool.create_pr(
        app, issue_number=1, title="t", body="b"
    )
    assert not resp.success
    assert resp.code == ErrorCode.PR_EXISTS


async def test_gh_missing_maps_to_git_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When gh isn't on PATH, create_pr returns GIT_ERROR — no fallback."""

    app = _make_app(tmp_path)
    _patch_branches(monkeypatch, default="main", current="feature/x")

    async def _not_installed(repo: str, **kwargs: Any) -> Any:
        raise GhUnavailable("gh CLI is not on PATH; install from https://cli.github.com/")

    monkeypatch.setattr(gh_cli, "create_pull_request", _not_installed)

    resp = await pr_tool.create_pr(
        app, issue_number=1, title="t", body="b"
    )
    assert not resp.success
    assert resp.code == ErrorCode.GIT_ERROR
    assert "gh" in (resp.error or "").lower()


async def test_generic_gh_failure_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-PR-exists gh failure still surfaces with its structured code."""

    app = _make_app(tmp_path)
    _patch_branches(monkeypatch, default="main", current="feature/x")
    _patch_gh_cli_create(
        monkeypatch,
        raises=GhAuthError(
            ErrorCode.INVALID_INPUT,
            "error: head branch must be pushed first",
        ),
    )

    resp = await pr_tool.create_pr(
        app, issue_number=1, title="t", body="b"
    )
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT


# ----------------------------------------------------------------------
# Return-shape smoke
# ----------------------------------------------------------------------


async def test_success_returns_url_and_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    _patch_branches(monkeypatch, default="main", current="feature/x")
    _patch_gh_cli_create(
        monkeypatch,
        result={
            "number": 7,
            "html_url": "https://github.com/octo/hello/pull/7",
            "draft": False,
            "head": "feature/x",
            "base": "main",
        },
    )

    resp = await pr_tool.create_pr(
        app, issue_number=5, title="my title", body="body text"
    )
    assert resp.success, resp.error
    assert resp.data["url"] == "https://github.com/octo/hello/pull/7"
    assert resp.data["number"] == 7
    assert resp.data["head"] == "feature/x"
    assert resp.data["base"] == "main"
    assert "Closes #5" in resp.data["body_used"]
