"""TRD-020-TEST — terminal fallback picker.

Strategy: monkey-patch :func:`rich.prompt.Prompt.ask` to script user
input, and patch :func:`ghia.tools.issues.list_issues` (re-exported on
the terminal module) to return a deterministic issue list.  No
subprocess, no real stdin — fast and hermetic.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterator

import pytest

from ghia import redaction
from ghia.app import GhiaApp, create_app
from ghia.errors import ErrorCode, err, ok
from ghia.ui import terminal as terminal_picker


_REPO = "octo/hello"


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    root = logging.getLogger()
    before = list(root.filters)
    redaction.set_token(None)
    yield
    for f in list(root.filters):
        if f not in before:
            root.removeFilter(f)
    redaction.set_token(None)


def _write_config(path: Path, **overrides: Any) -> None:
    """v0.2 per-repo config — no token, no repo field."""

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


def _make_issues(numbers: list[int]) -> list[dict[str, Any]]:
    return [
        {
            "number": n,
            "title": f"issue {n}",
            "body": "",
            "labels": [],
            "html_url": f"https://github.com/octo/hello/issues/{n}",
            "created_at": "2026-04-01T00:00:00Z",
            "updated_at": "2026-04-02T00:00:00Z",
            "author": "alice",
            "assignees": [],
            "comments_count": 0,
            "priority": "normal",
        }
        for n in numbers
    ]


def _patch_list(monkeypatch: pytest.MonkeyPatch, numbers: list[int]) -> None:
    issues = _make_issues(numbers)

    async def fake_list_issues(_app, label=None):
        return ok({"issues": issues, "count": len(issues)})

    monkeypatch.setattr(terminal_picker, "list_issues", fake_list_issues)


def _scripted_prompt(
    monkeypatch: pytest.MonkeyPatch, answers: list[str]
) -> Iterator[str]:
    """Replace Prompt.ask with a closure that walks ``answers`` in order."""

    iterator = iter(answers)

    def fake_ask(*_args, **_kwargs):
        try:
            return next(iterator)
        except StopIteration:
            return ""

    monkeypatch.setattr(terminal_picker.Prompt, "ask", staticmethod(fake_ask))
    return iterator


# ----------------------------------------------------------------------


async def test_selects_subset_of_issues(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_list(monkeypatch, [1, 2, 3])
    _scripted_prompt(monkeypatch, ["1,3", "semi"])

    result = await terminal_picker.pick_issues_terminal(app)

    assert result == {"queue": [1, 3], "mode": "semi"}


async def test_empty_input_returns_empty_queue(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_list(monkeypatch, [1, 2, 3])
    _scripted_prompt(monkeypatch, ["", "semi"])

    result = await terminal_picker.pick_issues_terminal(app)

    assert result == {"queue": [], "mode": "semi"}


async def test_garbage_input_does_not_raise(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_list(monkeypatch, [1, 2, 3])
    _scripted_prompt(monkeypatch, ["abc, ;; --, !!", "semi"])

    result = await terminal_picker.pick_issues_terminal(app)

    assert result == {"queue": [], "mode": "semi"}


async def test_numbers_not_in_issue_list_are_filtered_out(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_list(monkeypatch, [1, 2, 3])
    # 99 isn't an open issue — must be silently dropped rather than queued.
    _scripted_prompt(monkeypatch, ["1, 99, 2", "semi"])

    result = await terminal_picker.pick_issues_terminal(app)

    assert result == {"queue": [1, 2], "mode": "semi"}


async def test_dedupes_repeated_numbers(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_list(monkeypatch, [1, 2, 3])
    _scripted_prompt(monkeypatch, ["2, 2, 1, 2", "semi"])

    result = await terminal_picker.pick_issues_terminal(app)

    assert result == {"queue": [2, 1], "mode": "semi"}


async def test_mode_can_be_changed_to_full(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_list(monkeypatch, [1])
    _scripted_prompt(monkeypatch, ["1", "full"])

    result = await terminal_picker.pick_issues_terminal(app)

    assert result == {"queue": [1], "mode": "full"}


async def test_no_issues_returns_empty_without_prompting(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the repo has no open issues we don't even ask."""

    _patch_list(monkeypatch, [])

    # If Prompt.ask got called we'd raise from the patched fn:
    def boom(*_a, **_k):
        raise AssertionError("Prompt.ask should not be called when no issues")

    monkeypatch.setattr(terminal_picker.Prompt, "ask", staticmethod(boom))

    result = await terminal_picker.pick_issues_terminal(app)

    assert result == {"queue": [], "mode": "semi"}


async def test_list_issues_failure_returns_empty_without_raising(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(_app, label=None):
        return err(ErrorCode.RATE_LIMITED, "no quota")

    monkeypatch.setattr(terminal_picker, "list_issues", failing)

    result = await terminal_picker.pick_issues_terminal(app)

    assert result == {"queue": [], "mode": "semi"}


async def test_list_issues_raising_returns_empty_without_propagating(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The picker swallows tool exceptions so the MCP server keeps running."""

    async def boom(_app, label=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(terminal_picker, "list_issues", boom)

    result = await terminal_picker.pick_issues_terminal(app)

    assert result == {"queue": [], "mode": "semi"}


async def test_mode_default_honours_app_config_when_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the user just hits ENTER on mode, app.config.mode is the default."""

    cfg_path = tmp_path / "cfg.json"
    _write_config(cfg_path, mode="full")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    app = await create_app(
        repo_root=repo_root, config_path=cfg_path, repo_full_name=_REPO
    )

    _patch_list(monkeypatch, [1, 2])

    captured_defaults: list[str] = []

    def fake_ask(*_args, **kwargs):
        # The 2nd ask call (mode) carries the default we care about.
        if "default" in kwargs:
            captured_defaults.append(str(kwargs["default"]))
        # selection prompt -> "", mode prompt -> default
        return kwargs.get("default", "")

    monkeypatch.setattr(terminal_picker.Prompt, "ask", staticmethod(fake_ask))

    result = await terminal_picker.pick_issues_terminal(app)

    assert result["mode"] == "full"
    assert "full" in captured_defaults


async def test_parse_selection_unit_skips_non_digits() -> None:
    queue, ignored = terminal_picker._parse_selection(
        "1, abc, 2, 3.5, 4", {1, 2, 4}
    )
    assert queue == [1, 2, 4]
    # 'abc' and '3.5' both ignored.
    assert "abc" in ignored
    assert "3.5" in ignored


async def test_parse_selection_unit_handles_whitespace_and_commas() -> None:
    queue, ignored = terminal_picker._parse_selection(
        "  1   2 , 3,, 4  ", {1, 2, 3, 4}
    )
    assert queue == [1, 2, 3, 4]
    assert ignored == []
