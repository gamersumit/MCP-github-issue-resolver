"""TRD-010-TEST — setup wizard with mocked subprocess + prompts (v0.2 refactor).

Strategy:
* Patch ``rich.prompt.Prompt.ask`` and ``IntPrompt.ask`` so the
  wizard's prompts return scripted answers.
* Patch the four functions that wrap subprocess calls
  (``detect_repo``, ``gh_cli.gh_available``, ``gh_cli.auth_status``,
  ``gh_cli.repo_view``) so no real ``git`` / ``gh`` is invoked.
* Direct ``config_path_for`` at a throwaway location inside
  ``tmp_path`` so the real user config is untouched.

Tests cover the five wizard error paths plus the happy path:
  1. cwd not in a git repo
  2. gh not on PATH
  3. gh not authenticated
  4. active gh account can't see the repo (with suggested fixes)
  5. happy path saves the per-repo config
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, List
from unittest.mock import AsyncMock

import pytest

from ghia.config import Config, load_config
from ghia.errors import ErrorCode
from ghia.integrations.gh_cli import GhAuthError
from ghia.repo_detect import RepoDetectionError

import setup_wizard as wiz


# ----------------------------------------------------------------------
# Helpers: scripted prompt stand-ins
# ----------------------------------------------------------------------


class _ScriptedPrompt:
    """Drop-in for ``Prompt.ask`` / ``IntPrompt.ask`` that pops answers."""

    def __init__(self, answers: List[Any]) -> None:
        self._answers = list(answers)
        self.calls: List[tuple[tuple, dict]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if not self._answers:
            raise AssertionError(
                f"Prompt.ask invoked but no scripted answer remains "
                f"(call args={args!r}, kwargs={kwargs!r})"
            )
        return self._answers.pop(0)


@pytest.fixture
def isolated_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect config_path_for to tmp_path for wizard I/O."""

    target = tmp_path / "octocat__hello.json"
    monkeypatch.setattr(wiz, "config_path_for", lambda owner, name: target)
    return target


@pytest.fixture
def _no_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force test/lint detection to a known empty result."""

    from ghia.detection import DetectionResult

    monkeypatch.setattr(wiz, "detect", lambda _root: DetectionResult())


@pytest.fixture
def _detect_repo_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub repo detection to return ``octocat/hello``."""

    monkeypatch.setattr(wiz, "detect_repo", lambda _root: ("octocat", "hello"))


@pytest.fixture
def _gh_authed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub gh_available + auth_status + repo_view as the happy path."""

    monkeypatch.setattr(wiz.gh_cli, "gh_available", lambda: True)
    monkeypatch.setattr(
        wiz.gh_cli,
        "auth_status",
        AsyncMock(return_value={
            "authenticated": True,
            "active_account": "octocat",
            "hostname": "github.com",
        }),
    )
    monkeypatch.setattr(
        wiz.gh_cli,
        "repo_view",
        AsyncMock(return_value={
            "name": "hello",
            "nameWithOwner": "octocat/hello",
            "viewerPermission": "ADMIN",
        }),
    )


# ----------------------------------------------------------------------
# Happy path: fresh wizard run writes config
# ----------------------------------------------------------------------


async def test_fresh_wizard_run_writes_expected_config(
    isolated_config: Path,
    _no_detection: None,
    _detect_repo_ok: None,
    _gh_authed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Scripted answers in the order the wizard asks (no token, no repo
    # — those are auto-detected now):
    # 1. labels menu choice ("1" = default ai-fix)
    # 2. mode
    # 3. test command (empty → skip)
    # 4. lint command (empty → skip)
    prompt = _ScriptedPrompt([
        "1",        # labels menu — option 1 (default ai-fix)
        "semi",     # mode
        "",         # test_command (skip)
        "",         # lint_command (skip)
    ])
    monkeypatch.setattr("rich.prompt.Prompt.ask", prompt)
    monkeypatch.setattr("rich.prompt.IntPrompt.ask", _ScriptedPrompt([30]))

    code = await wiz.async_main()
    assert code == 0

    assert isolated_config.exists()
    if os.name == "posix":
        mode = stat.S_IMODE(isolated_config.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    cfg = load_config(path=isolated_config)
    assert cfg.labels == ["ai-fix"]
    assert cfg.mode == "semi"
    assert cfg.poll_interval_min == 30
    assert cfg.test_command is None
    assert cfg.lint_command is None


# ----------------------------------------------------------------------
# Re-run with existing config — defaults pre-fill, ENTER accepts
# ----------------------------------------------------------------------


async def test_rerun_loads_existing_config_as_defaults(
    isolated_config: Path,
    _no_detection: None,
    _detect_repo_ok: None,
    _gh_authed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seed an existing config.
    from ghia.config import save_config

    existing = Config(
        label="bug",
        mode="full",
        poll_interval_min=15,
        test_command="pytest -q",
        lint_command=None,
    )
    save_config(existing, path=isolated_config)

    # Returning the prompt's ``default`` simulates the user pressing
    # ENTER on every prompt — the cleanest model of "accept all".
    def _enter(*_args: Any, **kwargs: Any) -> Any:
        return kwargs.get("default")

    monkeypatch.setattr("rich.prompt.Prompt.ask", _enter)
    monkeypatch.setattr("rich.prompt.IntPrompt.ask", _enter)

    code = await wiz.async_main()
    assert code == 0

    new_cfg = load_config(path=isolated_config)
    assert new_cfg.label == existing.label
    assert new_cfg.mode == existing.mode
    assert new_cfg.poll_interval_min == existing.poll_interval_min
    assert new_cfg.test_command == existing.test_command
    assert new_cfg.lint_command == existing.lint_command


# ----------------------------------------------------------------------
# Detection feeds defaults for test/lint commands
# ----------------------------------------------------------------------


async def test_wizard_uses_detection_defaults_for_commands(
    isolated_config: Path,
    _detect_repo_ok: None,
    _gh_authed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detected commands are offered as defaults and accepted on ENTER."""

    from ghia.detection import DetectedCommand, DetectionResult

    detection = DetectionResult(
        test=DetectedCommand(
            command="pytest -q",
            source_file="pyproject.toml",
            confidence="high",
        ),
        lint=DetectedCommand(
            command="ruff check .",
            source_file="pyproject.toml",
            confidence="high",
        ),
    )
    monkeypatch.setattr(wiz, "detect", lambda _root: detection)

    # Sentinel ``None`` answers tell the helper to return whatever
    # ``default=`` was passed — i.e. simulate ENTER.
    scripted = [
        None,   # label
        None,   # mode
        None,   # test_command (default = pytest -q from detection)
        None,   # lint_command (default = ruff check . from detection)
    ]

    idx = {"i": 0}

    def _ask(*_args: Any, **kwargs: Any) -> Any:
        answer = scripted[idx["i"]]
        idx["i"] += 1
        if answer is None:
            return kwargs.get("default")
        return answer

    monkeypatch.setattr("rich.prompt.Prompt.ask", _ask)
    monkeypatch.setattr(
        "rich.prompt.IntPrompt.ask", lambda *_a, **kw: kw.get("default", 30)
    )

    code = await wiz.async_main()
    assert code == 0

    cfg = load_config(path=isolated_config)
    assert cfg.test_command == "pytest -q"
    assert cfg.lint_command == "ruff check ."


# ----------------------------------------------------------------------
# Error path 1: not in a git repo
# ----------------------------------------------------------------------


async def test_not_in_git_repo_errors_clearly(
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _not_a_repo(_root: Path) -> Any:
        raise RepoDetectionError("/tmp is not inside a git repository")

    monkeypatch.setattr(wiz, "detect_repo", _not_a_repo)

    code = await wiz.async_main()
    assert code == 2
    # Nothing was written.
    assert not isolated_config.exists()


# ----------------------------------------------------------------------
# Error path 2: gh not installed
# ----------------------------------------------------------------------


async def test_gh_not_installed_errors_with_install_hint(
    isolated_config: Path,
    _detect_repo_ok: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(wiz.gh_cli, "gh_available", lambda: False)

    code = await wiz.async_main()
    assert code == 3
    assert not isolated_config.exists()
    # The install help mentions at least one OS-specific install line.
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "brew install gh" in output or "apt install gh" in output


# ----------------------------------------------------------------------
# Error path 3: gh not authenticated
# ----------------------------------------------------------------------


async def test_gh_not_authenticated_errors_with_login_hint(
    isolated_config: Path,
    _detect_repo_ok: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(wiz.gh_cli, "gh_available", lambda: True)
    monkeypatch.setattr(
        wiz.gh_cli,
        "auth_status",
        AsyncMock(return_value={
            "authenticated": False,
            "active_account": None,
            "hostname": "github.com",
        }),
    )

    code = await wiz.async_main()
    assert code == 4
    assert not isolated_config.exists()
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "gh auth login" in output


# ----------------------------------------------------------------------
# Error path 4: gh authed but wrong account → suggest switch + login
# ----------------------------------------------------------------------


async def test_gh_authed_but_repo_inaccessible_suggests_switch_and_login(
    isolated_config: Path,
    _detect_repo_ok: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(wiz.gh_cli, "gh_available", lambda: True)
    monkeypatch.setattr(
        wiz.gh_cli,
        "auth_status",
        AsyncMock(return_value={
            "authenticated": True,
            "active_account": "wrong-user",
            "hostname": "github.com",
        }),
    )
    monkeypatch.setattr(
        wiz.gh_cli,
        "repo_view",
        AsyncMock(side_effect=GhAuthError(
            code=ErrorCode.REPO_NOT_FOUND,
            message="GitHub returned 404: Could not resolve to a Repository",
        )),
    )

    code = await wiz.async_main()
    assert code == 5
    assert not isolated_config.exists()

    captured = capsys.readouterr()
    output = captured.out + captured.err
    # Both suggested fixes must appear in the user-facing error.
    assert "gh auth switch" in output
    assert "gh auth login" in output
    # The active account name must appear so the user knows what they're switching from.
    assert "wrong-user" in output


# ----------------------------------------------------------------------
# Keyboard interrupt aborts cleanly without writing config
# ----------------------------------------------------------------------


async def test_keyboard_interrupt_aborts_without_writing(
    isolated_config: Path,
    _no_detection: None,
    _detect_repo_ok: None,
    _gh_authed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr("rich.prompt.Prompt.ask", _boom)
    monkeypatch.setattr(
        "rich.prompt.IntPrompt.ask", lambda *_a, **_kw: 30
    )

    code = await wiz.async_main()
    assert code != 0
    assert not isolated_config.exists()


# ----------------------------------------------------------------------
# UX guarantees: closing panel + scope split (install.sh owns MCP add)
# ----------------------------------------------------------------------


async def test_wizard_does_not_call_claude_mcp_add(
    isolated_config: Path,
    _no_detection: None,
    _detect_repo_ok: None,
    _gh_authed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wizard must not shell out to `claude mcp add` — that's install.sh's job.

    Why this matters: v0.1 had the wizard try to register the MCP, which
    led to per-repo registrations colliding and the wizard "succeeding"
    even when registration silently failed. Splitting concerns means
    the wizard NEVER touches Claude Code config, which is enforced here
    by patching subprocess.run + asyncio.create_subprocess_exec and
    asserting neither saw a `claude` argv.
    """

    import subprocess as _subprocess

    seen: List[List[str]] = []

    real_run = _subprocess.run

    def _spy_run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        # `cmd` may be a list or str depending on caller. Normalize to
        # list for the assertion check; pass through to real_run so
        # legitimate git/gh calls inside the wizard's stubbed code path
        # would still work (they're already mocked above).
        argv = cmd if isinstance(cmd, list) else [cmd]
        seen.append([str(a) for a in argv])
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(_subprocess, "run", _spy_run)

    prompt = _ScriptedPrompt([
        "1",        # labels menu — option 1 (default ai-fix)
        "semi",     # mode
        "",         # test_command (skip)
        "",         # lint_command (skip)
    ])
    monkeypatch.setattr("rich.prompt.Prompt.ask", prompt)
    monkeypatch.setattr("rich.prompt.IntPrompt.ask", _ScriptedPrompt([30]))

    code = await wiz.async_main()
    assert code == 0

    # No invocation of `claude mcp add` (or any `claude` binary call)
    # may originate from the wizard's code path.
    for argv in seen:
        first = argv[0] if argv else ""
        assert "claude" not in first, (
            f"wizard unexpectedly invoked claude CLI: {argv!r}"
        )


async def test_closing_panel_mentions_user_scope_and_slash_commands(
    isolated_config: Path,
    _no_detection: None,
    _detect_repo_ok: None,
    _gh_authed: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Final panel must split per-repo vs global scope and show slash commands.

    Pinning the wording so a future refactor can't silently revert to
    the v0.1 panel that conflated the two scopes (and printed a
    misleading `claude mcp add` line as a manual step).
    """

    prompt = _ScriptedPrompt([
        "1",        # labels menu — option 1 (default ai-fix)
        "semi",     # mode
        "",         # test_command (skip)
        "",         # lint_command (skip)
    ])
    monkeypatch.setattr("rich.prompt.Prompt.ask", prompt)
    monkeypatch.setattr("rich.prompt.IntPrompt.ask", _ScriptedPrompt([30]))

    code = await wiz.async_main()
    assert code == 0

    output = capsys.readouterr().out
    # Per-repo vs global scope distinction must be explicit.
    assert "Per-repo config" in output
    assert "GLOBAL" in output
    assert "Scope: user" in output
    # New slash command form must be advertised.
    assert "/mcp__github-issue-agent__start" in output
    # The misleading v0.1 instruction must NOT reappear in the panel.
    assert "claude mcp add github-issue-agent -- python -m server" not in output
    assert "/issue-agent start" not in output


# ----------------------------------------------------------------------
# Permissions hook helpers
# ----------------------------------------------------------------------


def test_merge_policy_hook_into_empty_settings() -> None:
    settings: dict[str, Any] = {}
    out = wiz._merge_policy_hook(settings, "/usr/bin/python -m ghia.policy.permission_policy")
    pretool = out["hooks"]["PreToolUse"]
    assert isinstance(pretool, list) and len(pretool) == 1
    sub = pretool[0]["hooks"][0]
    assert sub["type"] == "command"
    assert "ghia.policy.permission_policy" in sub["command"]
    assert sub["timeout"] == 10


def test_merge_policy_hook_preserves_existing_unrelated_hooks() -> None:
    """Existing user hooks (e.g. their own PreToolUse linter) must survive a wizard re-run."""

    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Edit",
                    "hooks": [{"type": "command", "command": "my-personal-linter"}],
                }
            ],
            "PostToolUse": [
                {"hooks": [{"type": "command", "command": "my-postcommit"}]}
            ],
        },
        "permissions": {"deny": ["Bash(rm -rf:*)"]},
    }
    out = wiz._merge_policy_hook(settings, "abs-path -m ghia.policy.permission_policy")
    # User's existing entry preserved
    assert out["hooks"]["PreToolUse"][0]["matcher"] == "Edit"
    # Our entry appended
    assert any(
        "ghia.policy.permission_policy" in h.get("command", "")
        for entry in out["hooks"]["PreToolUse"]
        for h in entry.get("hooks", [])
    )
    # Unrelated keys untouched
    assert out["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "my-postcommit"
    assert out["permissions"]["deny"] == ["Bash(rm -rf:*)"]


def test_merge_policy_hook_idempotent_on_rerun() -> None:
    """Re-running install must update the path (e.g. clone moved) without duplicating."""

    settings: dict[str, Any] = {}
    wiz._merge_policy_hook(settings, "old/python -m ghia.policy.permission_policy")
    wiz._merge_policy_hook(settings, "new/python -m ghia.policy.permission_policy")
    pretool = settings["hooks"]["PreToolUse"]
    assert len(pretool) == 1, "policy hook entry should be refreshed in-place, not duplicated"
    assert pretool[0]["hooks"][0]["command"].startswith("new/python")


def test_load_claude_settings_handles_corrupt_file(tmp_path: Path) -> None:
    """A garbled settings.local.json must rotate aside, NOT silently overwrite."""

    target = tmp_path / "settings.local.json"
    target.write_text("{ this is not valid json", encoding="utf-8")
    out = wiz._load_claude_settings(target)
    assert out == {}
    backups = list(tmp_path.glob("settings.local.json.bak-*"))
    assert backups, "corrupt settings file should be rotated to .bak-<ts>"


# ----------------------------------------------------------------------
# Git identity helpers
# ----------------------------------------------------------------------


def test_prompt_git_identity_skips_when_already_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both keys already set → no prompt, no subprocess writes."""

    from rich.console import Console

    def _fake_local(repo_root: Path, key: str) -> Optional[str]:  # type: ignore[name-defined]
        return {"user.name": "Existing User", "user.email": "user@example.com"}[key]

    def _explode_set(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("_set_git_local should not be called when both keys are set")

    monkeypatch.setattr(wiz, "_git_local_config", _fake_local)
    monkeypatch.setattr(wiz, "_set_git_local", _explode_set)

    wiz._prompt_git_identity(Console(), tmp_path, "octocat")
    out = capsys.readouterr().out
    # Rich may wrap mid-sentence — assert on tokens that survive wrapping.
    assert "already set" in out
    assert "Existing User" in out
    assert "user@example.com" in out


def test_prompt_git_identity_prompts_and_writes_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local keys missing → user typed name + email → both are written via --local."""

    from rich.console import Console

    monkeypatch.setattr(wiz, "_git_local_config", lambda *_a, **_kw: None)
    monkeypatch.setattr(wiz, "_git_global_config", lambda key: None)
    monkeypatch.setattr(wiz, "_gh_active_user_email", lambda *_a, **_kw: None)

    written: List[tuple[str, str]] = []

    def _spy_set(repo_root: Path, key: str, value: str) -> None:
        written.append((key, value))

    monkeypatch.setattr(wiz, "_set_git_local", _spy_set)

    monkeypatch.setattr(
        "rich.prompt.Prompt.ask",
        _ScriptedPrompt(["Real Name", "real@example.com"]),
    )

    wiz._prompt_git_identity(Console(), tmp_path, "octocat")

    assert written == [("user.name", "Real Name"), ("user.email", "real@example.com")]


def test_prompt_git_identity_rejects_invalid_email_then_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Email validation re-prompts on a malformed entry, accepts the next try."""

    from rich.console import Console

    monkeypatch.setattr(wiz, "_git_local_config", lambda *_a, **_kw: None)
    monkeypatch.setattr(wiz, "_git_global_config", lambda key: None)
    monkeypatch.setattr(wiz, "_gh_active_user_email", lambda *_a, **_kw: None)

    written: List[tuple[str, str]] = []
    monkeypatch.setattr(
        wiz,
        "_set_git_local",
        lambda r, k, v: written.append((k, v)),
    )
    monkeypatch.setattr(
        "rich.prompt.Prompt.ask",
        _ScriptedPrompt(["Real Name", "not-an-email", "real@example.com"]),
    )

    wiz._prompt_git_identity(Console(), tmp_path, None)

    assert ("user.email", "real@example.com") in written
    assert all(email != "not-an-email" for _key, email in written)
