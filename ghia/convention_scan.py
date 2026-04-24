"""Convention discovery (TRD-014).

Scans the repo root for well-known "project rules" files and stitches
their first ~2 KB into a single summary string for injection into the
agent protocol.  Runs in a worker thread so the event loop is never
blocked by filesystem I/O.

File set (matches REQ-020b):

* ``CLAUDE.md``        — Claude Code project instructions
* ``AGENTS.md``        — generic AI-agent conventions
* ``CONTRIBUTING.md``  — human contribution guide
* ``.cursor/rules/*.md`` — Cursor IDE rule files (globbed)
* ``.editorconfig``    — style hints
* ``README.md``        — top-level overview

Satisfies REQ-020b.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Iterable, List

logger = logging.getLogger(__name__)

__all__ = ["discover_conventions"]


# Per-file cap — we prefer breadth over depth, so no single file gets
# to eat the whole budget.  The TRD spec says "first ~500 chars or the
# first heading's content, whichever is larger up to 2 KB cap".  We
# implement that by reading 2 KB raw, then if a Markdown heading exists
# we keep everything up to the next heading (capped again at 2 KB).
_PER_FILE_CAP = 2048

# Total output cap across all files.
_TOTAL_CAP = 8192

_TRUNCATED = "...[truncated]"


# Ordered so the most agent-relevant files appear first in the rendered
# summary — CLAUDE.md and AGENTS.md are the most prescriptive, so they
# dominate the budget if the repo contains many conventions.
_FIXED_FILES: tuple[str, ...] = (
    "CLAUDE.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    ".editorconfig",
    "README.md",
)


def _candidate_paths(repo_root: Path) -> List[Path]:
    """Return the existing convention file paths in priority order.

    ``.cursor/rules/*.md`` is a glob — any number of files may exist,
    and we include all of them after the fixed list.  Non-existent
    candidates are filtered out so callers don't pay the cost of
    reading them.
    """

    candidates: List[Path] = []
    for name in _FIXED_FILES:
        path = repo_root / name
        if path.is_file():
            candidates.append(path)

    cursor_dir = repo_root / ".cursor" / "rules"
    if cursor_dir.is_dir():
        # Sort for deterministic ordering across platforms.
        for path in sorted(cursor_dir.glob("*.md")):
            if path.is_file():
                candidates.append(path)
    return candidates


def _safe_read(path: Path) -> str:
    """Read a file as UTF-8 with error replacement.

    Convention files are nearly always text, but ``.editorconfig`` in
    the wild is sometimes saved with odd encodings, and tests
    exercise the "binary junk" case too.  ``errors="replace"``
    produces a valid string (with replacement characters) for any
    byte content, which is exactly what we want for a summary that
    will be shown to a model, not parsed.
    """

    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("could not read convention file %s: %s", path, exc)
        return ""


def _extract_snippet(content: str) -> str:
    """Return up to ``_PER_FILE_CAP`` chars from ``content``.

    Strategy:
      1. If the document begins with a Markdown heading, we want to
         include the whole first section (``# Heading\n...body...``
         up to the next heading or EOF), capped at the per-file cap.
      2. Otherwise we return the first 500 chars — enough to give the
         model a flavor of the file without blowing the budget.
      3. In both cases we apply the hard per-file cap and append the
         truncation marker if we cut content.
    """

    if not content:
        return ""

    stripped = content.lstrip()
    if stripped.startswith("#"):
        # Find the second top-level heading; everything before it is
        # the first section.  We only treat ATX-style headings because
        # convention files are almost always Markdown.
        lines = content.splitlines()
        seen_first = False
        cut_idx = len(lines)
        for idx, line in enumerate(lines):
            if line.lstrip().startswith("#"):
                if seen_first:
                    cut_idx = idx
                    break
                seen_first = True
        first_section = "\n".join(lines[:cut_idx])
        # TRD: "first 500 chars or first heading's content, whichever
        # is larger up to 2 KB cap" — so we take the max of 500 and
        # the section length.
        budget = max(500, len(first_section))
        budget = min(budget, _PER_FILE_CAP)
    else:
        budget = min(500, _PER_FILE_CAP)

    if len(content) <= budget:
        return content
    return content[:budget] + _TRUNCATED


def _render_section(rel_name: str, snippet: str) -> str:
    """Format one file's snippet with a clear filename header."""

    return f"### {rel_name}\n\n{snippet}\n"


def _render(repo_root: Path, paths: Iterable[Path]) -> str:
    """Stitch per-file sections together under the total budget."""

    sections: List[str] = []
    running_len = 0
    for path in paths:
        snippet = _extract_snippet(_safe_read(path))
        if not snippet:
            continue
        try:
            rel = path.relative_to(repo_root).as_posix()
        except ValueError:
            # Shouldn't happen — all candidates live under repo_root —
            # but fall back gracefully.
            rel = path.name
        section = _render_section(rel, snippet)

        # Enforce the total cap: if adding the whole section would
        # overflow we truncate the section body to fit (minus the
        # marker) so the output ends cleanly.
        remaining = _TOTAL_CAP - running_len
        if remaining <= 0:
            break
        if len(section) > remaining:
            # Keep the header but clip the body; leave room for the
            # truncation marker.
            cut = max(0, remaining - len(_TRUNCATED))
            sections.append(section[:cut] + _TRUNCATED)
            running_len = _TOTAL_CAP
            break
        sections.append(section)
        running_len += len(section)

    return "".join(sections).rstrip("\n")


def _discover_sync(repo_root: Path) -> str:
    """Synchronous core — runs in a worker thread via ``to_thread``."""

    if not repo_root.is_dir():
        logger.debug("repo_root %s is not a directory; no conventions", repo_root)
        return ""
    paths = _candidate_paths(repo_root)
    if not paths:
        return ""
    return _render(repo_root, paths)


async def discover_conventions(repo_root: Path) -> str:
    """Return a summary of project-convention files found under ``repo_root``.

    The returned string is ready to drop straight into the protocol
    template.  When no recognized files exist the empty string is
    returned so the template can render its own "none detected"
    sentinel copy.

    Runs the synchronous scan in a worker thread so the event loop
    remains responsive even on slow filesystems.
    """

    return await asyncio.to_thread(_discover_sync, Path(repo_root))
