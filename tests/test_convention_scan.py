"""TRD-014-TEST — verify convention discovery.

Covers:
* CLAUDE.md alone produces a section with filename header + content
* Multiple convention files are all included; total under 8 KB
* No convention files → empty string returned
* Binary / junk bytes do not crash the reader (``errors="replace"``)
* Per-file cap (~2 KB) is enforced with a truncation marker
* ``.cursor/rules/*.md`` glob is included
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ghia.convention_scan import discover_conventions


async def test_empty_repo_returns_empty_string(tmp_path: Path) -> None:
    assert await discover_conventions(tmp_path) == ""


async def test_nonexistent_repo_root_returns_empty(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert await discover_conventions(missing) == ""


async def test_claude_md_included_with_header(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text(
        "# Project Rules\n\nAlways be kind to tests.\n"
    )
    out = await discover_conventions(tmp_path)

    assert "### CLAUDE.md" in out
    assert "Always be kind to tests." in out


async def test_multiple_files_all_included(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text(
        "# Claude\n\nLine A.\n"
    )
    (tmp_path / "CONTRIBUTING.md").write_text(
        "# Contributing\n\nLine B.\n"
    )
    (tmp_path / "AGENTS.md").write_text(
        "# Agents\n\nLine C.\n"
    )
    (tmp_path / "README.md").write_text(
        "# Readme\n\nLine D.\n"
    )

    out = await discover_conventions(tmp_path)
    assert "### CLAUDE.md" in out
    assert "### CONTRIBUTING.md" in out
    assert "### AGENTS.md" in out
    assert "### README.md" in out
    assert "Line A." in out
    assert "Line B." in out
    assert "Line C." in out
    assert "Line D." in out

    # Hard cap: 8 KB.
    assert len(out) < 8192 + 64  # small slack for the truncation marker


async def test_total_cap_enforced(tmp_path: Path) -> None:
    """Huge files combined must be clipped at 8 KB with a marker."""

    # Each file is 2 KB-ish of content; four of them would exceed
    # 8 KB total if per-file and total caps weren't enforced.
    big = "x" * 5000
    for name in ("CLAUDE.md", "CONTRIBUTING.md", "AGENTS.md", "README.md"):
        (tmp_path / name).write_text(big)

    out = await discover_conventions(tmp_path)
    assert len(out) <= 8192 + 32  # total cap + small marker slack
    assert "[truncated]" in out


async def test_per_file_cap_clips_long_files(tmp_path: Path) -> None:
    """A single 10 KB file is clipped at ~2 KB with a marker."""

    huge = "y" * 10_000
    (tmp_path / "CLAUDE.md").write_text(huge)

    out = await discover_conventions(tmp_path)
    # Section is header + snippet + marker; snippet itself <= 2048
    assert "[truncated]" in out
    # The entire output stays well below the 8 KB total cap.
    assert len(out) < 3000


async def test_binary_junk_does_not_crash(tmp_path: Path) -> None:
    """Binary-looking bytes must be read with error replacement."""

    path = tmp_path / "CLAUDE.md"
    # Random bytes including invalid UTF-8 sequences.
    path.write_bytes(b"\xff\xfe\x00\xc3\x28 legit ascii \xa0\xa1")

    out = await discover_conventions(tmp_path)
    # No exception; the file is acknowledged in the output.
    assert "### CLAUDE.md" in out
    assert "legit ascii" in out


async def test_cursor_rules_glob_included(tmp_path: Path) -> None:
    rules_dir = tmp_path / ".cursor" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "python.md").write_text(
        "# Python\n\nUse type hints.\n"
    )
    (rules_dir / "style.md").write_text(
        "# Style\n\nTabs are evil.\n"
    )

    out = await discover_conventions(tmp_path)
    assert "### .cursor/rules/python.md" in out
    assert "### .cursor/rules/style.md" in out
    assert "Use type hints." in out
    assert "Tabs are evil." in out


async def test_editorconfig_included(tmp_path: Path) -> None:
    (tmp_path / ".editorconfig").write_text(
        "root = true\n[*]\nindent_style = space\n"
    )
    out = await discover_conventions(tmp_path)
    assert "### .editorconfig" in out
    assert "indent_style = space" in out


async def test_file_order_deterministic(tmp_path: Path) -> None:
    """Fixed-file list always produces the same order."""

    for name in ("CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md"):
        (tmp_path / name).write_text(f"# {name}\n")

    out1 = await discover_conventions(tmp_path)
    out2 = await discover_conventions(tmp_path)
    assert out1 == out2
    # CLAUDE comes before AGENTS comes before CONTRIBUTING (priority order).
    assert out1.index("### CLAUDE.md") < out1.index("### AGENTS.md")
    assert out1.index("### AGENTS.md") < out1.index("### CONTRIBUTING.md")
