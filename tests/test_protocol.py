"""TRD-013-TEST — verify agent-protocol renderer.

Covers:
* mode=semi renders the semi block and drops the full block
* mode=full renders the full block and drops the semi block
* {repo}, {mode}, {discovered_conventions} placeholders substitute
* Non-render-time placeholders (e.g. ``{number}``, ``{issue_title}``)
  are preserved verbatim for Claude to interpret at runtime
* Missing template file raises ``ProtocolTemplateError`` (not a silent
  empty string)
* ``format_queue_summary`` produces a bullet list or empty string
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ghia import protocol as protocol_mod
from ghia.protocol import (
    ProtocolTemplateError,
    format_queue_summary,
    render_protocol,
    template_path,
)


def _render(**overrides: str) -> str:
    """Render the protocol with sensible defaults plus overrides."""

    defaults = {
        "repo": "octo/hello",
        "mode": "semi",
        "default_branch": "main",
        "discovered_conventions": "CONVS",
        "queue_summary": "- #1: (title unknown until fetched)",
        "timestamp": "2026-04-23 12:00 UTC",
    }
    defaults.update(overrides)
    return render_protocol(**defaults)  # type: ignore[arg-type]


def test_template_exists_in_repo() -> None:
    """Sanity check: the template actually ships with the repo."""

    assert template_path().is_file(), (
        f"expected template at {template_path()}"
    )


def test_semi_mode_renders_semi_block_only() -> None:
    out = _render(mode="semi")
    assert "SEMI-AUTO mode (current)" in out
    assert "FULL-AUTO mode (current)" not in out


def test_full_mode_renders_full_block_only() -> None:
    out = _render(mode="full")
    assert "FULL-AUTO mode (current)" in out
    assert "SEMI-AUTO mode (current)" not in out


def test_placeholders_substitute_at_render_time() -> None:
    out = _render(
        repo="alice/widgets",
        mode="full",
        default_branch="develop",
        discovered_conventions="**Custom rules**",
        timestamp="2026-04-23 09:15 UTC",
    )
    assert "alice/widgets" in out
    assert "Mode: full" in out
    assert "Default branch: develop" in out
    assert "**Custom rules**" in out
    assert "2026-04-23 09:15 UTC" in out


def test_runtime_placeholders_preserved_verbatim() -> None:
    """``{number}`` is for Claude to fill in, not us — must survive."""

    out = _render(mode="semi")
    # The semi arm contains "fix/issue-{number}-{short-slug}" —
    # both tokens must still be single-brace literals in the output.
    assert "{number}" in out
    assert "{short-slug}" in out


def test_rules_section_present_in_both_modes() -> None:
    for m in ("semi", "full"):
        out = _render(mode=m)
        assert "## Rules (both modes)" in out
        assert "Never commit on the default branch" in out
        assert "## Error handling" in out


def test_empty_conventions_renders_fallback_copy() -> None:
    out = _render(discovered_conventions="")
    assert "(none detected)" in out


def test_missing_template_raises_clear_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point the loader at a path that doesn't exist and demand a loud error."""

    missing = tmp_path / "no_such_template.md"
    monkeypatch.setattr(protocol_mod, "template_path", lambda: missing)

    with pytest.raises(ProtocolTemplateError) as info:
        _render()
    # Cause should be FileNotFoundError for the "truly missing" case.
    assert isinstance(info.value.__cause__, FileNotFoundError)


def test_queue_summary_empty() -> None:
    assert format_queue_summary([]) == ""


def test_queue_summary_with_items() -> None:
    summary = format_queue_summary([42, 7, 101])
    lines = summary.splitlines()
    assert len(lines) == 3
    assert lines[0] == "- #42: (title unknown until fetched)"
    assert lines[1] == "- #7: (title unknown until fetched)"


def test_queue_fallback_copy_when_empty() -> None:
    """``render_protocol`` swaps in a helpful placeholder for empty queues."""

    out = _render(queue_summary="")
    assert "(queue empty" in out


def test_jinja_block_syntax_not_leaked_to_output() -> None:
    """The ``{% if %}`` markers must be stripped after rendering."""

    out = _render(mode="semi")
    assert "{% if" not in out
    assert "{% endif" not in out
    assert "{{%" not in out
