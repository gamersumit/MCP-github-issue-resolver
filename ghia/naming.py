"""Naming helpers (TRD-032).

Pure functions — no I/O, no globals, no clock — that turn an issue
number plus a human title into the deterministic identifiers we use
everywhere downstream:

* git branch names (machine-readable, slug form)
* commit messages (human-readable, original casing preserved)
* PR titles (human-readable, original casing preserved)

Centralizing them here means TRD-024 (PR creation), TRD-023
(``create_branch``), and the protocol template (TRD-013/033) all agree
on the same string shapes — which is the whole point of REQ-021.

Satisfies REQ-021.
"""

from __future__ import annotations

import re

__all__ = ["slugify", "branch_name", "commit_msg", "pr_title"]


# Anything that isn't ASCII alnum becomes a single dash; this is
# deliberately lossy (unicode letters → dashes) because git refnames
# and shell-friendliness matter more than non-ASCII fidelity for the
# slug portion.  The original title is still preserved in commit/PR
# messages, so no information is lost end-to-end.
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")

# Fallback when a title slugifies to the empty string (e.g. "!!!" or
# pure non-ASCII).  Callers expect a non-empty slug so the resulting
# branch name still has a stable, unique-ish shape.
_EMPTY_SLUG_FALLBACK = "issue"


def slugify(title: str, max_len: int = 40) -> str:
    """Lowercase, dash-separated, length-capped slug.

    Steps (in order):

    1. lowercase
    2. collapse any run of non-alphanumerics into a single ``-``
    3. strip leading/trailing dashes
    4. truncate to ``max_len`` characters
    5. strip trailing dashes again — truncation can land on a dash and
       we never want a trailing one in the final slug
    6. if the result is empty, fall back to ``"issue"`` so callers
       always get a non-empty slug

    Args:
        title: Free-form issue title.  May be empty.
        max_len: Hard cap on the returned length (default 40).

    Returns:
        Slug string of at most ``max_len`` characters; never empty.
    """

    # Defensive: callers pass GitHub data that should always be str,
    # but a None or numeric slip-through shouldn't crash slugify.
    if not isinstance(title, str):
        title = str(title or "")

    lowered = title.lower()
    dashed = _NON_ALNUM.sub("-", lowered)
    trimmed = dashed.strip("-")
    # Cap to max_len; a non-positive cap collapses to empty and falls
    # through to the fallback below.
    cap = max(0, max_len)
    trimmed = trimmed[:cap]
    # Re-strip trailing dashes — truncation could have introduced one,
    # e.g. "fix-the-bug-now"[:11] == "fix-the-bug" (good) but
    # "fix-bug-now"[:8] == "fix-bug-" (bad without this re-strip).
    trimmed = trimmed.rstrip("-")
    return trimmed or _EMPTY_SLUG_FALLBACK


def branch_name(issue_number: int, title: str) -> str:
    """Canonical fix-branch name: ``fix-issue-{n}-{slug}``.

    Used by ``create_branch`` (TRD-023) and the local-branch duplicate
    detector (TRD-017) — both must agree, hence the single source of
    truth here.
    """

    return f"fix-issue-{issue_number}-{slugify(title)}"


def commit_msg(issue_number: int, title: str) -> str:
    """Canonical commit subject: ``fix(#N): <original-cased title>``.

    Commit messages are read by humans (and by ``git log`` / PR
    diffs), so we preserve original casing rather than feeding through
    ``slugify``.  An empty title degrades to ``fix(#N)`` so the message
    is still a valid conventional-commit prefix.
    """

    if not isinstance(title, str):
        title = str(title or "")
    stripped = title.strip()
    if not stripped:
        return f"fix(#{issue_number})"
    return f"fix(#{issue_number}): {stripped}"


def pr_title(issue_number: int, title: str) -> str:
    """Canonical PR title: ``Fix #N: <original-cased title>``.

    Mirrors :func:`commit_msg` but uses the GitHub-friendly
    ``Fix #N`` prefix that auto-links to the issue in the PR list.
    Empty title degrades to ``Fix #N``.
    """

    if not isinstance(title, str):
        title = str(title or "")
    stripped = title.strip()
    if not stripped:
        return f"Fix #{issue_number}"
    return f"Fix #{issue_number}: {stripped}"
