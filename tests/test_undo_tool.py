"""Undo-tool tests (TRD-028-TEST).

Real-git tests using a per-test repo (``git init`` + author config).
The whole module skips when ``git`` isn't on PATH so constrained
runners still pass.

Coverage:
* refuses on protected default branch
* refuses on a foreign-author commit
* happy path: HEAD moves back by one commit; working tree reflects
  the rollback; structured payload includes both SHAs
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from ghia.app import GhiaApp
from ghia.config import Config
from ghia.errors import ErrorCode
from ghia.session import SessionStore
from ghia.tools import undo as undo_tool


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not on PATH",
)


_AGENT_EMAIL = "ghia-test@example.com"
_AGENT_NAME = "ghia test"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _make_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", _AGENT_EMAIL)
    _git(repo, "config", "user.name", _AGENT_NAME)
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "initial")


def _make_app(repo: Path, tmp_path: Path) -> GhiaApp:
    cfg = Config(label="ai-fix", mode="semi", poll_interval_min=30)
    return GhiaApp(
        config=cfg,
        session=SessionStore(tmp_path / "session.json"),
        repo_root=repo,
        repo_full_name="octo/hello",
        logger=logging.getLogger("ghia-test-undo"),
    )


@pytest.fixture
def app(tmp_path: Path) -> GhiaApp:
    repo = tmp_path / "repo"
    _make_repo(repo)
    return _make_app(repo, tmp_path)


# ----------------------------------------------------------------------
# Refusal cases
# ----------------------------------------------------------------------


async def test_refuses_on_default_branch(app: GhiaApp) -> None:
    """We're sitting on main — undo must refuse regardless of authorship."""

    resp = await undo_tool.undo_last_change(app)
    assert not resp.success
    assert resp.code == ErrorCode.UNDO_REFUSED_PROTECTED_BRANCH


async def test_refuses_on_foreign_commit(app: GhiaApp) -> None:
    repo = app.repo_root
    _git(repo, "switch", "-c", "feature/foreign")
    (repo / "x.txt").write_text("foreign\n")
    _git(repo, "add", "x.txt")
    # Commit with a different author identity than the configured
    # user.email — undo must refuse.
    _git(
        repo,
        "-c",
        "user.email=outsider@example.com",
        "-c",
        "user.name=outsider",
        "commit",
        "-q",
        "-m",
        "outsider commit",
        "--author=outsider <outsider@example.com>",
    )

    resp = await undo_tool.undo_last_change(app)
    assert not resp.success
    assert resp.code == ErrorCode.UNDO_REFUSED_NOT_OURS


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


async def test_happy_path_resets_one_commit(app: GhiaApp) -> None:
    repo = app.repo_root
    _git(repo, "switch", "-c", "feature/agent")
    (repo / "y.txt").write_text("agent change\n")
    _git(repo, "add", "y.txt")
    _git(repo, "commit", "-q", "-m", "agent commit")

    # Capture HEAD SHAs before / after for a sanity check on the
    # response payload.
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    parent = _git(repo, "rev-parse", "HEAD~1").stdout.strip()

    resp = await undo_tool.undo_last_change(app)
    assert resp.success, resp.error
    assert resp.data["undone_sha"] == before
    assert resp.data["new_head_sha"] == parent

    # HEAD really moved back.
    after = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert after == parent
    # Hard reset removed the working-tree change too.
    assert not (repo / "y.txt").exists()


async def test_default_branch_check_runs_before_authorship(
    app: GhiaApp,
) -> None:
    """Even when authorship would pass, default-branch refusal wins."""

    # Stay on main; the initial commit was authored by the agent
    # identity, so authorship would pass.  Default-branch must
    # still refuse.
    resp = await undo_tool.undo_last_change(app)
    assert not resp.success
    assert resp.code == ErrorCode.UNDO_REFUSED_PROTECTED_BRANCH
