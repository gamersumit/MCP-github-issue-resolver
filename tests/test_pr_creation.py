"""PR creation tests (TRD-024-TEST).

We mock at two boundaries:

* ``subprocess.run`` for the ``gh`` CLI path (assert exact argv).
* ``ghia.tools.pr._get_client`` for the PyGithub fallback path.

The git default-branch / current-branch lookups also go through
``_run_git`` — we monkeypatch the git module's ``_run_git`` so the
tests don't need a real git binary.

Coverage:
* mode='full' default → --draft passed
* mode='semi' default → no --draft
* explicit draft flag wins over mode
* body without close-marker gets one appended
* body with existing Fixes #N preserved (no duplicate)
* refusal on default branch
* PyGithub fallback when gh is missing
* PR_EXISTS mapping when gh stderr says so
* PR_EXISTS mapping when PyGithub raises 422 / "already exists"
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
from ghia.integrations.github import GitHubClientError
from ghia.session import SessionState, SessionStore
from ghia.tools import git as git_tools
from ghia.tools import pr as pr_tool


def _make_app(tmp_path: Path, *, mode: str = "semi") -> GhiaApp:
    cfg = Config(
        token="ghp_" + "x" * 36,
        repo="octo/hello",
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


def _patch_subprocess_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: str = "https://github.com/octo/hello/pull/42\n",
    stderr: str = "",
) -> dict[str, Any]:
    """Mock subprocess.run inside the pr module; capture argv."""

    captured: dict[str, Any] = {}

    def _fake_run(argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv, returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr(pr_tool.subprocess, "run", _fake_run)
    monkeypatch.setattr(pr_tool.shutil, "which", lambda _: "/usr/bin/gh")
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
    captured = _patch_subprocess_run(monkeypatch)

    resp = await pr_tool.create_pr(
        app, issue_number=1, title="t", body="b"
    )
    assert resp.success, resp.error
    assert resp.data["draft"] is True
    assert "--draft" in captured["argv"]


async def test_semi_mode_defaults_to_non_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, mode="semi")
    await app.session.update(mode="semi")
    _patch_branches(monkeypatch, default="main", current="feature/x")
    captured = _patch_subprocess_run(monkeypatch)

    resp = await pr_tool.create_pr(
        app, issue_number=1, title="t", body="b"
    )
    assert resp.success, resp.error
    assert resp.data["draft"] is False
    assert "--draft" not in captured["argv"]


async def test_explicit_draft_overrides_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, mode="full")
    await app.session.update(mode="full")
    _patch_branches(monkeypatch, default="main", current="feature/x")
    captured = _patch_subprocess_run(monkeypatch)

    resp = await pr_tool.create_pr(
        app, issue_number=1, title="t", body="b", draft=False
    )
    assert resp.success, resp.error
    assert resp.data["draft"] is False
    assert "--draft" not in captured["argv"]


# ----------------------------------------------------------------------
# Body / Closes-marker handling
# ----------------------------------------------------------------------


async def test_body_appends_closes_marker_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    _patch_branches(monkeypatch, default="main", current="feature/x")
    captured = _patch_subprocess_run(monkeypatch)

    await pr_tool.create_pr(
        app, issue_number=42, title="t", body="some description"
    )
    # Pull --body's value out of argv.
    argv = captured["argv"]
    body_idx = argv.index("--body") + 1
    body_val = argv[body_idx]
    assert "Closes #42" in body_val


async def test_body_preserves_existing_closes_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    _patch_branches(monkeypatch, default="main", current="feature/x")
    captured = _patch_subprocess_run(monkeypatch)

    user_body = "see also Fixes #42 inline"
    await pr_tool.create_pr(
        app, issue_number=42, title="t", body=user_body
    )
    argv = captured["argv"]
    body_idx = argv.index("--body") + 1
    body_val = argv[body_idx]
    # Original body preserved, no duplicate "Closes #42" appended.
    assert "Fixes #42" in body_val
    assert "Closes #42" not in body_val


async def test_body_marker_uses_correct_issue_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Fixes #99`` should NOT satisfy the marker for issue #42."""

    app = _make_app(tmp_path)
    _patch_branches(monkeypatch, default="main", current="feature/x")
    captured = _patch_subprocess_run(monkeypatch)

    await pr_tool.create_pr(
        app, issue_number=42, title="t", body="Fixes #99"
    )
    argv = captured["argv"]
    body_val = argv[argv.index("--body") + 1]
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
    # Subprocess must NOT be invoked when we refuse — patch with a
    # raise to assert non-call.
    monkeypatch.setattr(pr_tool.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(
        pr_tool.subprocess,
        "run",
        lambda *a, **kw: pytest.fail("subprocess must not be called"),
    )

    resp = await pr_tool.create_pr(
        app, issue_number=1, title="t", body="b"
    )
    assert not resp.success
    assert resp.code == ErrorCode.ON_DEFAULT_BRANCH_REFUSED


async def test_pr_exists_maps_correctly_via_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    _patch_branches(monkeypatch, default="main", current="feature/dup")
    _patch_subprocess_run(
        monkeypatch,
        returncode=1,
        stdout="",
        stderr="a pull request for branch \"feature/dup\" into branch \"main\" already exists\n",
    )

    resp = await pr_tool.create_pr(
        app, issue_number=1, title="t", body="b"
    )
    assert not resp.success
    assert resp.code == ErrorCode.PR_EXISTS


async def test_gh_other_failure_maps_to_git_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    _patch_branches(monkeypatch, default="main", current="feature/x")
    _patch_subprocess_run(
        monkeypatch,
        returncode=1,
        stdout="",
        stderr="error: head branch must be pushed first\n",
    )

    resp = await pr_tool.create_pr(
        app, issue_number=1, title="t", body="b"
    )
    assert not resp.success
    assert resp.code == ErrorCode.GIT_ERROR


# ----------------------------------------------------------------------
# PyGithub fallback
# ----------------------------------------------------------------------


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_with: GitHubClientError | None = None
        self.return_value: dict[str, Any] = {
            "number": 7,
            "html_url": "https://github.com/octo/hello/pull/7",
            "draft": False,
            "head": "feature/x",
            "base": "main",
        }

    async def create_pull_request(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.raise_with is not None:
            raise self.raise_with
        return dict(self.return_value)


async def test_pygithub_fallback_when_gh_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path, mode="full")
    await app.session.update(mode="full")
    _patch_branches(monkeypatch, default="main", current="feature/x")

    monkeypatch.setattr(pr_tool.shutil, "which", lambda _: None)
    fake = _FakeClient()
    monkeypatch.setattr(pr_tool, "_get_client", lambda app: fake)

    resp = await pr_tool.create_pr(
        app, issue_number=5, title="my title", body="body text"
    )
    assert resp.success, resp.error
    # The fallback was actually used.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["head"] == "feature/x"
    assert call["base"] == "main"
    assert call["title"] == "my title"
    assert "Closes #5" in call["body"]
    assert call["draft"] is True  # full mode


async def test_pygithub_fallback_pr_exists_mapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    _patch_branches(monkeypatch, default="main", current="feature/dup")

    monkeypatch.setattr(pr_tool.shutil, "which", lambda _: None)
    fake = _FakeClient()
    fake.raise_with = GitHubClientError(
        ErrorCode.NETWORK_ERROR,
        "Validation Failed: A pull request already exists for octo:feature/dup",
    )
    monkeypatch.setattr(pr_tool, "_get_client", lambda app: fake)

    resp = await pr_tool.create_pr(
        app, issue_number=1, title="t", body="b"
    )
    assert not resp.success
    assert resp.code == ErrorCode.PR_EXISTS
