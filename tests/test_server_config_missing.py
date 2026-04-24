"""TRD UX-fix: CONFIG_MISSING error message includes repo name + setup command.

The v0.1 server returned a generic CONFIG_MISSING with the hint
"run python -m setup_wizard from the repo dir". Two problems:
 1. The hint named the wrong invocation (the new console script is
    ``github-issue-agent-setup``).
 2. The hint never named the repo, so a user with multiple repos open
    couldn't tell which config was missing.

These tests pin both fixes against regression. We construct a real
git repo in tmp_path with a github.com origin so ``detect_repo``
succeeds, then point ``Path.cwd()`` at it and ensure no per-repo
config exists — the only way to drive the ConfigMissingError branch
of ``_get_app_or_error`` end-to-end.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

import server
from ghia.errors import ErrorCode


@pytest.fixture
def fresh_app_state() -> None:
    """Reset server module-globals so each test sees a fresh lazy init.

    ``server._app`` is cached after the first successful call. Tests
    that drive the error path need the cache empty AND need to clear
    it again afterward so subsequent tests don't inherit a stale
    error/state.
    """

    server._app = None
    yield
    server._app = None


def _init_git_repo(root: Path, origin_url: str) -> None:
    """Create a minimal git repo with a github.com origin URL.

    We avoid any commits because ``detect_repo`` only needs
    ``git rev-parse --show-toplevel`` + ``git remote get-url origin``
    to succeed — both of which work on an empty repo as long as the
    remote is configured.
    """

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(root)],
        check=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", "origin", origin_url],
        check=True,
        env=env,
    )


@pytest.fixture
def empty_repo_no_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_app_state: None
) -> tuple[Path, str]:
    """A git repo on disk with NO per-repo config + cwd pointing at it."""

    repo = tmp_path / "demo"
    repo.mkdir()
    _init_git_repo(repo, "git@github.com:acme-corp/demo-repo.git")

    # Direct config_path_for at an empty tmp dir so the wizard's real
    # config (if any) on the developer machine never bleeds into the
    # test. We don't need to write anything — we want the "missing"
    # path. Patch BOTH the config module and the server-imported alias
    # if the server ever gains its own import (defense in depth).
    config_dir = tmp_path / "config" / "repos"
    config_dir.mkdir(parents=True)

    from ghia import config as ghia_config

    monkeypatch.setattr(
        ghia_config,
        "default_config_dir",
        lambda: config_dir,
    )

    monkeypatch.chdir(repo)
    return repo, "acme-corp/demo-repo"


def test_config_missing_error_includes_repo_name(
    empty_repo_no_config: tuple[Path, str],
) -> None:
    """The error message must name THIS repo, not "this repo" generic.

    Why specific name matters: a user with multiple Claude Code
    sessions open across repos can't act on a generic message. Naming
    the slug makes the next step ("run the wizard for THAT repo")
    obvious.
    """

    _repo_path, repo_full = empty_repo_no_config

    app, err = asyncio.get_event_loop().run_until_complete(
        server._get_app_or_error()
    )
    assert app is None
    assert err is not None
    assert err.success is False
    assert err.code == ErrorCode.CONFIG_MISSING
    assert repo_full in (err.error or ""), (
        f"expected repo slug {repo_full!r} in error message, got {err.error!r}"
    )


def test_config_missing_error_includes_setup_command(
    empty_repo_no_config: tuple[Path, str],
) -> None:
    """The error must give the EXACT command the user should run.

    Why "github-issue-agent-setup" specifically: that's the console
    script registered in pyproject.toml; the older suggestion
    ``python -m setup_wizard`` required the user to know the venv
    path, which contradicts the streamlined-UX goal.
    """

    app, err = asyncio.get_event_loop().run_until_complete(
        server._get_app_or_error()
    )
    assert err is not None
    msg = err.error or ""
    assert "github-issue-agent-setup" in msg, (
        f"expected literal command name in error message; got: {msg!r}"
    )
    # And must NOT regress to the v0.1 wording.
    assert "python -m setup_wizard" not in msg


def test_config_missing_falls_back_gracefully_when_repo_undetectable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh_app_state: None,
) -> None:
    """If detection ALSO fails, the user still sees an actionable hint.

    Edge case: cwd briefly stopped being a git repo between the first
    and second detection call (e.g. someone deleted .git mid-session).
    The handler must not crash; it should still give the
    setup-command instruction with a generic repo label.
    """

    # cwd is a non-repo tmp dir, so detect_repo will raise both times.
    monkeypatch.chdir(tmp_path)

    # Make create_app() fail with ConfigMissingError so we drive the
    # right branch (otherwise we'd hit the RepoDetectionError branch).
    # NB: patch the alias bound on the server module — server.py does
    # ``from ghia.app import create_app`` so monkeypatching the source
    # module wouldn't affect the resolved name.
    from ghia.config import ConfigMissingError

    async def _boom(*_a, **_kw):
        raise ConfigMissingError("simulated missing config")

    monkeypatch.setattr(server, "create_app", _boom)

    _app, err = asyncio.get_event_loop().run_until_complete(
        server._get_app_or_error()
    )
    assert err is not None
    assert err.code == ErrorCode.CONFIG_MISSING
    msg = err.error or ""
    assert "github-issue-agent-setup" in msg
    # Generic fallback label still readable.
    assert "this repo" in msg or "/" in msg
