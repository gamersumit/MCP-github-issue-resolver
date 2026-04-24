"""Git tool tests (TRD-023-TEST).

Real-git tests use a per-test repo created by the ``git_repo`` fixture
(``git init`` + author config + initial commit).  Tests are skipped
when the ``git`` binary isn't on ``PATH`` so the suite still runs in
constrained CI.

Coverage:
* get_default_branch: main / master / develop / no-candidates / cache reuse
* get_current_branch
* create_branch: happy path, invalid name, single & double collision
* git_diff
* commit_changes: refuse on default branch, empty msg, happy path
* push_branch: refuse on default branch (mocked, never actually push)
* GIT_NOT_FOUND when binary is missing (monkeypatched)
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

from ghia.app import GhiaApp
from ghia.config import Config
from ghia.errors import ErrorCode
from ghia.session import SessionStore
from ghia.tools import git as git_tools


# Skip the whole module if git isn't installed — these tests need a
# real git binary, no mocking the subprocess away.
pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not on PATH",
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command synchronously; raises if it fails.

    Helper used by fixtures to set up the repo state — production code
    goes through ``_run_git`` (async) instead.
    """

    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _make_repo_with_branch(repo: Path, branch: str) -> None:
    """``git init`` ``repo`` and put it on ``branch`` with one commit."""

    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "config", "user.email", "ghia-test@example.com")
    _git(repo, "config", "user.name", "ghia test")
    # Avoid global ``commit.gpgsign`` interference in CI.
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "initial")


def _make_app(repo: Path, tmp_path: Path) -> GhiaApp:
    cfg = Config(label="ai-fix", mode="semi", poll_interval_min=30)
    session_path = tmp_path / "session.json"
    return GhiaApp(
        config=cfg,
        session=SessionStore(session_path),
        repo_root=repo,
        repo_full_name="octo/hello",
        logger=logging.getLogger("ghia-test-git"),
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A fresh ``main``-default git repo under ``tmp_path/repo``."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo_with_branch(repo, "main")
    return repo


@pytest.fixture
def app(git_repo: Path, tmp_path: Path) -> GhiaApp:
    return _make_app(git_repo, tmp_path)


# ----------------------------------------------------------------------
# get_default_branch
# ----------------------------------------------------------------------


@pytest.mark.parametrize("branch", ["main", "master", "develop"])
async def test_get_default_branch_detects_local_candidates(
    branch: str, tmp_path: Path
) -> None:
    repo = tmp_path / f"repo-{branch}"
    repo.mkdir()
    _make_repo_with_branch(repo, branch)
    app = _make_app(repo, tmp_path)

    resp = await git_tools.get_default_branch(app)
    assert resp.success, resp.error
    assert resp.data["default_branch"] == branch


async def test_get_default_branch_no_candidates_errors(tmp_path: Path) -> None:
    """Empty repo with a non-standard branch and no remote → no detection."""

    repo = tmp_path / "weird-repo"
    repo.mkdir()
    _make_repo_with_branch(repo, "wat-is-this")
    app = _make_app(repo, tmp_path)

    resp = await git_tools.get_default_branch(app)
    assert not resp.success
    assert resp.code == ErrorCode.NO_DEFAULT_BRANCH_DETECTED


async def test_get_default_branch_caches_in_session(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second call must read from session cache, not re-shell."""

    first = await git_tools.get_default_branch(app)
    assert first.success
    assert first.data["default_branch"] == "main"

    state = await app.session.read()
    assert state.default_branch == "main"

    # Patch _run_git to explode if called again; the cache should
    # short-circuit BEFORE we ever shell out.
    async def _boom(*_a: Any, **_k: Any) -> tuple[int, str, str]:
        raise AssertionError("get_default_branch should not reshell when cached")

    monkeypatch.setattr(git_tools, "_run_git", _boom)

    second = await git_tools.get_default_branch(app)
    assert second.success
    assert second.data["default_branch"] == "main"


# ----------------------------------------------------------------------
# get_current_branch
# ----------------------------------------------------------------------


async def test_get_current_branch_returns_main_initially(app: GhiaApp) -> None:
    resp = await git_tools.get_current_branch(app)
    assert resp.success
    assert resp.data["current_branch"] == "main"


async def test_get_current_branch_after_switch(
    app: GhiaApp, git_repo: Path
) -> None:
    _git(git_repo, "switch", "-c", "feature/foo")
    resp = await git_tools.get_current_branch(app)
    assert resp.success
    assert resp.data["current_branch"] == "feature/foo"


# ----------------------------------------------------------------------
# create_branch
# ----------------------------------------------------------------------


async def test_create_branch_happy_path_creates_and_switches(
    app: GhiaApp, git_repo: Path
) -> None:
    resp = await git_tools.create_branch(app, "fix-issue-1-foo")
    assert resp.success, resp.error
    assert resp.data["branch"] == "fix-issue-1-foo"
    assert resp.data["created"] is True

    current = await git_tools.get_current_branch(app)
    assert current.data["current_branch"] == "fix-issue-1-foo"


async def test_create_branch_invalid_name_with_space_rejected(
    app: GhiaApp,
) -> None:
    resp = await git_tools.create_branch(app, "bad name")
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT


async def test_create_branch_invalid_name_with_shell_meta_rejected(
    app: GhiaApp,
) -> None:
    for bad in ("foo;rm -rf /", "foo$bar", "foo|bar", "foo`x`"):
        resp = await git_tools.create_branch(app, bad)
        assert not resp.success, f"expected reject for {bad!r}"
        assert resp.code == ErrorCode.INVALID_INPUT


async def test_create_branch_collision_appends_v2(
    app: GhiaApp, git_repo: Path
) -> None:
    # Pre-create the original branch so the next attempt collides.
    _git(git_repo, "branch", "existing-branch")

    resp = await git_tools.create_branch(app, "existing-branch")
    assert resp.success, resp.error
    assert resp.data["branch"] == "existing-branch-v2"


async def test_create_branch_double_collision_appends_v3(
    app: GhiaApp, git_repo: Path
) -> None:
    _git(git_repo, "branch", "existing-branch")
    _git(git_repo, "branch", "existing-branch-v2")

    resp = await git_tools.create_branch(app, "existing-branch")
    assert resp.success, resp.error
    assert resp.data["branch"] == "existing-branch-v3"


async def test_create_branch_all_suffixes_taken_returns_branch_exists(
    app: GhiaApp, git_repo: Path
) -> None:
    """When name + -v2..-v9 all exist we must report BRANCH_EXISTS."""

    _git(git_repo, "branch", "x")
    for n in range(2, 10):
        _git(git_repo, "branch", f"x-v{n}")

    resp = await git_tools.create_branch(app, "x")
    assert not resp.success
    assert resp.code == ErrorCode.BRANCH_EXISTS


# ----------------------------------------------------------------------
# git_diff
# ----------------------------------------------------------------------


async def test_git_diff_returns_diff_after_edit(
    app: GhiaApp, git_repo: Path
) -> None:
    # Move off the default branch so we can edit + diff freely.
    _git(git_repo, "switch", "-c", "feature/diff-test")
    (git_repo / "README.md").write_text("hi\nchanged\n")

    resp = await git_tools.git_diff(app)
    assert resp.success, resp.error
    assert "changed" in resp.data["diff"]
    assert resp.data["files_changed"] == 1


async def test_git_diff_staged_flag(app: GhiaApp, git_repo: Path) -> None:
    _git(git_repo, "switch", "-c", "feature/staged-test")
    (git_repo / "README.md").write_text("hi\nstaged change\n")
    _git(git_repo, "add", "README.md")

    worktree = await git_tools.git_diff(app)
    staged = await git_tools.git_diff(app, staged=True)

    # Staged diff should show the change; worktree diff should be empty.
    assert "staged change" in staged.data["diff"]
    assert worktree.data["diff"] == ""


async def test_git_diff_paths_filter(app: GhiaApp, git_repo: Path) -> None:
    _git(git_repo, "switch", "-c", "feature/paths")
    (git_repo / "README.md").write_text("readme change\n")
    (git_repo / "other.txt").write_text("other\n")

    resp = await git_tools.git_diff(app, paths=["README.md"])
    assert resp.success
    assert "README.md" in resp.data["diff"]
    assert "other.txt" not in resp.data["diff"]


# ----------------------------------------------------------------------
# commit_changes
# ----------------------------------------------------------------------


async def test_commit_changes_refuses_on_default_branch(app: GhiaApp) -> None:
    """We're sitting on ``main`` (the detected default) — must refuse."""

    resp = await git_tools.commit_changes(app, "any message")
    assert not resp.success
    assert resp.code == ErrorCode.ON_DEFAULT_BRANCH_REFUSED


async def test_commit_changes_empty_message_rejected(
    app: GhiaApp, git_repo: Path
) -> None:
    _git(git_repo, "switch", "-c", "feature/empty-msg")
    resp = await git_tools.commit_changes(app, "   ")
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT


async def test_commit_changes_happy_path(
    app: GhiaApp, git_repo: Path
) -> None:
    _git(git_repo, "switch", "-c", "feature/commit-happy")
    (git_repo / "README.md").write_text("hi\nedit\n")

    resp = await git_tools.commit_changes(app, "fix: edit readme")
    assert resp.success, resp.error
    assert resp.data["files_changed"] == 1
    assert resp.data["message"] == "fix: edit readme"
    # SHA must be 40-char hex.
    sha = resp.data["sha"]
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


async def test_commit_changes_with_explicit_paths(
    app: GhiaApp, git_repo: Path
) -> None:
    _git(git_repo, "switch", "-c", "feature/commit-paths")
    new_file = git_repo / "new.txt"
    new_file.write_text("new file\n")

    # ``add -u`` would NOT pick up the new untracked file; passing
    # paths explicitly must.
    resp = await git_tools.commit_changes(app, "add new.txt", paths=["new.txt"])
    assert resp.success, resp.error
    assert resp.data["files_changed"] == 1


async def test_commit_changes_nothing_staged_returns_invalid_input(
    app: GhiaApp, git_repo: Path
) -> None:
    _git(git_repo, "switch", "-c", "feature/empty-commit")
    resp = await git_tools.commit_changes(app, "no changes")
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT


# ----------------------------------------------------------------------
# push_branch
# ----------------------------------------------------------------------


async def test_push_branch_refuses_on_default_branch(app: GhiaApp) -> None:
    resp = await git_tools.push_branch(app)
    assert not resp.success
    assert resp.code == ErrorCode.ON_DEFAULT_BRANCH_REFUSED


async def test_push_branch_invokes_run_git_when_off_default(
    app: GhiaApp, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mock _run_git so we don't actually try to push to a remote."""

    _git(git_repo, "switch", "-c", "feature/push-mock")

    captured: list[tuple[str, ...]] = []

    real_run_git = git_tools._run_git

    async def _fake(app_in: GhiaApp, *args: str, **kw: Any) -> tuple[int, str, str]:
        captured.append(args)
        # Default-branch detection still uses _run_git for show-ref /
        # symbolic-ref / rev-parse — defer to the real impl for those
        # so our cache-priming and current-branch checks succeed.
        if args and args[0] != "push":
            return await real_run_git(app_in, *args, **kw)
        return 0, "", "branch 'feature/push-mock' set up to track 'origin/feature/push-mock'.\n"

    monkeypatch.setattr(git_tools, "_run_git", _fake)

    resp = await git_tools.push_branch(app)
    assert resp.success, resp.error
    # The push command must have been invoked with -u origin HEAD.
    push_calls = [a for a in captured if a and a[0] == "push"]
    assert push_calls
    assert push_calls[0] == ("push", "-u", "origin", "HEAD")
    assert resp.data["branch"] == "feature/push-mock"
    assert resp.data["remote"] == "origin"


async def test_push_branch_invalid_remote_name_rejected(app: GhiaApp) -> None:
    resp = await git_tools.push_branch(app, remote="bad name")
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT


# ----------------------------------------------------------------------
# git binary missing
# ----------------------------------------------------------------------


async def test_git_not_found_returns_structured_error(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkeypatch _run_git to raise FileNotFoundError (git missing)."""

    async def _missing(*_a: Any, **_k: Any) -> tuple[int, str, str]:
        raise FileNotFoundError("git")

    monkeypatch.setattr(git_tools, "_run_git", _missing)

    resp = await git_tools.get_current_branch(app)
    assert not resp.success
    assert resp.code == ErrorCode.GIT_NOT_FOUND


async def test_get_default_branch_short_circuits_when_git_missing(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First strategy attempt must return GIT_NOT_FOUND; no fallback probing."""

    call_count = 0

    async def _missing(*_a: Any, **_k: Any) -> tuple[int, str, str]:
        nonlocal call_count
        call_count += 1
        raise FileNotFoundError("git")

    monkeypatch.setattr(git_tools, "_run_git", _missing)

    resp = await git_tools.get_default_branch(app)
    assert not resp.success
    assert resp.code == ErrorCode.GIT_NOT_FOUND
    # We expect only the first strategy to fire and short-circuit
    # the rest — the others would just raise the same error again.
    assert call_count == 1
