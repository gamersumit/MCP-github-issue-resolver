"""TRD-010-TEST — end-to-end setup wizard with mocked prompts + network.

Strategy:
* Patch ``rich.prompt.Prompt.ask``, ``rich.prompt.IntPrompt.ask``, and
  ``rich.prompt.Confirm.ask`` inside the ``setup_wizard`` module so the
  wizard's prompts return scripted answers.
* Mock ``validate_token`` and ``check_repo_access`` in the wizard
  module so no real HTTP is issued.
* Direct ``default_config_path`` at a throwaway location inside
  ``tmp_path`` so the real user config is untouched.

Every test asserts both "wizard completes" and "no network hit" — the
mocked network functions are themselves asyncio coroutines, so the
test runner also exercises the wizard's await points.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Iterator, List
from unittest.mock import AsyncMock

import pytest

from ghia.config import Config, load_config, save_config
from ghia.github_client_light import TokenValidation

import setup_wizard as wiz


# ----------------------------------------------------------------------
# Helpers: scripted prompt stand-ins
# ----------------------------------------------------------------------


class _ScriptedPrompt:
    """Drop-in for ``Prompt.ask`` / ``IntPrompt.ask`` that pops answers.

    The wizard calls these in order; we keep a FIFO queue and yield the
    next answer each time ``.ask`` is invoked.  Type conversion (str vs
    int) is handled per-class so ``IntPrompt`` behaves realistically.
    """

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
    """Redirect default_config_path to tmp_path for wizard I/O."""

    target = tmp_path / "config.json"
    monkeypatch.setattr(wiz, "default_config_path", lambda: target)
    return target


@pytest.fixture
def _no_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force detection to a known empty result so prompt answers match."""

    from ghia.detection import DetectionResult

    monkeypatch.setattr(wiz, "detect", lambda _root: DetectionResult())


def _valid_token_validation(fine_grained: bool = False) -> TokenValidation:
    return TokenValidation(
        valid=True,
        user="octocat",
        scopes=[] if fine_grained else ["repo"],
        is_fine_grained=fine_grained,
        missing_scopes=[],
        error=None,
    )


def _invalid_token_validation(msg: str = "bad token") -> TokenValidation:
    return TokenValidation(
        valid=False,
        user=None,
        scopes=[],
        is_fine_grained=False,
        missing_scopes=[],
        error=msg,
    )


def _valid_repo_validation(full_name: str = "octocat/hello") -> TokenValidation:
    return TokenValidation(
        valid=True,
        user=full_name,
        scopes=[],
        is_fine_grained=True,
        missing_scopes=[],
        error=None,
    )


# ----------------------------------------------------------------------
# End-to-end: fresh run, fine-grained token
# ----------------------------------------------------------------------


async def test_fresh_wizard_run_writes_expected_config(
    isolated_config: Path,
    _no_detection: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Scripted answers in the order the wizard asks:
    # 1. token
    # 2. repo
    # 3. label
    # 4. mode
    # 5. test command (empty → skip)
    # 6. lint command (empty → skip)
    prompt = _ScriptedPrompt(
        [
            "github_pat_" + "a" * 40,  # token
            "octocat/hello",            # repo
            "ai-fix",                   # label
            "semi",                     # mode
            "",                         # test_command (skip)
            "",                         # lint_command (skip)
        ]
    )
    monkeypatch.setattr("rich.prompt.Prompt.ask", prompt)
    # Integer prompts: poll_interval_min
    int_prompt = _ScriptedPrompt([30])
    monkeypatch.setattr("rich.prompt.IntPrompt.ask", int_prompt)
    # Confirm: only fired if we ask to continue despite missing scopes —
    # the fine-grained path doesn't fire it, so an empty script is fine.
    confirm = _ScriptedPrompt([])
    monkeypatch.setattr("rich.prompt.Confirm.ask", confirm)

    # Network: both validators return "valid" on first try.
    monkeypatch.setattr(
        wiz,
        "validate_token",
        AsyncMock(return_value=_valid_token_validation(fine_grained=True)),
    )
    monkeypatch.setattr(
        wiz,
        "check_repo_access",
        AsyncMock(return_value=_valid_repo_validation("octocat/hello")),
    )

    code = await wiz.main()
    assert code == 0

    assert isolated_config.exists()
    # Permissions: POSIX chmod 600
    if os.name == "posix":
        mode = stat.S_IMODE(isolated_config.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    cfg = load_config(path=isolated_config)
    assert cfg.repo == "octocat/hello"
    assert cfg.label == "ai-fix"
    assert cfg.mode == "semi"
    assert cfg.poll_interval_min == 30
    assert cfg.test_command is None
    assert cfg.lint_command is None
    # Token round-trips as-is
    assert cfg.token.startswith("github_pat_")


# ----------------------------------------------------------------------
# Re-run with existing config — defaults pre-fill, ENTER accepts
# ----------------------------------------------------------------------


async def test_rerun_loads_existing_config_as_defaults(
    isolated_config: Path,
    _no_detection: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seed an existing config.
    existing = Config(
        token="github_pat_" + "e" * 40,
        repo="alice/widgets",
        label="bug",
        mode="full",
        poll_interval_min=15,
        test_command="pytest -q",
        lint_command=None,
    )
    save_config(existing, path=isolated_config)

    # Scripted ENTER behavior: Prompt.ask was called with default=<existing
    # value>, so the test returns the existing value to simulate ENTER.
    # We simply return whatever the wizard passed as ``default`` — the
    # cleanest simulation of a user hitting ENTER on every prompt.
    def _enter(*_args: Any, **kwargs: Any) -> Any:
        return kwargs.get("default")

    monkeypatch.setattr("rich.prompt.Prompt.ask", _enter)
    monkeypatch.setattr("rich.prompt.IntPrompt.ask", _enter)
    monkeypatch.setattr(
        "rich.prompt.Confirm.ask", lambda *a, **kw: kw.get("default", True)
    )

    monkeypatch.setattr(
        wiz,
        "validate_token",
        AsyncMock(return_value=_valid_token_validation(fine_grained=True)),
    )
    monkeypatch.setattr(
        wiz,
        "check_repo_access",
        AsyncMock(return_value=_valid_repo_validation("alice/widgets")),
    )

    code = await wiz.main()
    assert code == 0

    # After "ENTER through everything" re-run, config is byte-identical.
    new_cfg = load_config(path=isolated_config)
    assert new_cfg.repo == existing.repo
    assert new_cfg.label == existing.label
    assert new_cfg.mode == existing.mode
    assert new_cfg.poll_interval_min == existing.poll_interval_min
    assert new_cfg.test_command == existing.test_command
    assert new_cfg.lint_command == existing.lint_command
    assert new_cfg.token == existing.token


# ----------------------------------------------------------------------
# Bad token → loops until good token provided
# ----------------------------------------------------------------------


async def test_bad_token_then_good_token_loops(
    isolated_config: Path,
    _no_detection: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two token attempts: the first is the "bad" token, the second is good.
    # After the token loop, the rest of the wizard proceeds normally.
    prompt = _ScriptedPrompt(
        [
            "bad_token",                    # token attempt 1 → rejected
            "github_pat_" + "g" * 40,       # token attempt 2 → accepted
            "octocat/hello",                # repo
            "ai-fix",                       # label
            "semi",                         # mode
            "",                             # test
            "",                             # lint
        ]
    )
    monkeypatch.setattr("rich.prompt.Prompt.ask", prompt)
    monkeypatch.setattr("rich.prompt.IntPrompt.ask", _ScriptedPrompt([30]))
    monkeypatch.setattr("rich.prompt.Confirm.ask", _ScriptedPrompt([]))

    validate_mock = AsyncMock(
        side_effect=[
            _invalid_token_validation("Token rejected by GitHub (401)."),
            _valid_token_validation(fine_grained=True),
        ]
    )
    monkeypatch.setattr(wiz, "validate_token", validate_mock)
    monkeypatch.setattr(
        wiz,
        "check_repo_access",
        AsyncMock(return_value=_valid_repo_validation("octocat/hello")),
    )

    code = await wiz.main()
    assert code == 0

    # validate_token was called twice — bad then good
    assert validate_mock.await_count == 2

    cfg = load_config(path=isolated_config)
    assert cfg.token.startswith("github_pat_")


# ----------------------------------------------------------------------
# Wizard loads detection when present (smoke)
# ----------------------------------------------------------------------


async def test_wizard_uses_detection_defaults_for_commands(
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    # ENTER-through everything except token/repo which we scripted.
    # Scripted ordering:
    #   1. token          (typed)
    #   2. repo           (typed)
    #   3. label          (ENTER = ai-fix via default mechanism)
    #   4. mode           (ENTER = semi)
    #   5. test_command   (ENTER = pytest -q from detection)
    #   6. lint_command   (ENTER = ruff check . from detection)
    # Since ENTER returns the `default` kwarg passed in, we use that
    # adaptive helper for the ENTER answers.
    scripted = [
        "github_pat_" + "f" * 40,   # token
        "octocat/hello",             # repo
        None,                        # sentinel → use default for label
        None,                        # sentinel → use default for mode
        None,                        # sentinel → default for test cmd
        None,                        # sentinel → default for lint cmd
    ]

    idx = {"i": 0}

    def _ask(*args: Any, **kwargs: Any) -> Any:
        answer = scripted[idx["i"]]
        idx["i"] += 1
        if answer is None:
            return kwargs.get("default")
        return answer

    monkeypatch.setattr("rich.prompt.Prompt.ask", _ask)
    monkeypatch.setattr(
        "rich.prompt.IntPrompt.ask", lambda *a, **kw: kw.get("default", 30)
    )
    monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **kw: True)

    monkeypatch.setattr(
        wiz,
        "validate_token",
        AsyncMock(return_value=_valid_token_validation(fine_grained=True)),
    )
    monkeypatch.setattr(
        wiz,
        "check_repo_access",
        AsyncMock(return_value=_valid_repo_validation("octocat/hello")),
    )

    code = await wiz.main()
    assert code == 0

    cfg = load_config(path=isolated_config)
    assert cfg.test_command == "pytest -q"
    assert cfg.lint_command == "ruff check ."


# ----------------------------------------------------------------------
# Keyboard interrupt aborts cleanly without writing config
# ----------------------------------------------------------------------


async def test_keyboard_interrupt_aborts_without_writing(
    isolated_config: Path,
    _no_detection: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr("rich.prompt.Prompt.ask", _boom)
    monkeypatch.setattr(
        "rich.prompt.IntPrompt.ask", lambda *a, **kw: 30
    )
    monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **kw: True)

    monkeypatch.setattr(
        wiz,
        "validate_token",
        AsyncMock(return_value=_valid_token_validation(fine_grained=True)),
    )
    monkeypatch.setattr(
        wiz,
        "check_repo_access",
        AsyncMock(return_value=_valid_repo_validation()),
    )

    code = await wiz.main()
    assert code != 0
    assert not isolated_config.exists()
