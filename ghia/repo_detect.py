"""Repository auto-detection (v0.2 refactor).

Replaces the user-typed ``owner/name`` config field with detection from
``git remote get-url origin`` so the agent is implicitly per-repo: open
Claude Code in any git repo and the active repo is whatever ``origin``
points at.

Two helpers:

* :func:`parse_remote_url` — pure string parser (no I/O).  Accepts the
  three URL shapes git itself emits (SSH, SSH-config-alias, HTTPS) and
  rejects anything that isn't a github.com URL.  Pulled out so the
  parsing logic is unit-testable without spawning ``git``.
* :func:`detect_repo` — runs ``git -C <root> remote get-url origin``,
  feeds the output through :func:`parse_remote_url`, and returns the
  ``owner/name`` slug.  Surfaces structured errors when the cwd isn't a
  git repo, has no ``origin`` remote, or points at a non-github host.

The slug is always lowercase-preserved (we don't normalize case) — git
URLs and the GitHub API both accept case-insensitive lookups, but
returning the user's authoritative casing keeps log lines and config
filenames consistent with what they typed when they cloned.

Why ``__`` as the per-repo-config separator: ``/`` would create a
nested directory layout (``repos/owner/name.json``) that's harder to
``ls`` and harder to nuke; ``-`` collides with valid characters in
both owner and name parts (``rust-lang/rust-clippy`` would alias).
``__`` is unique to the separator role and survives round-trip
without quoting.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "RepoDetectionError",
    "parse_remote_url",
    "detect_repo",
    "config_filename_for",
]


class RepoDetectionError(Exception):
    """Raised when we can't extract a github.com ``owner/name`` slug.

    Wraps the four failure modes — not in a git repo, no ``origin``
    remote, non-github URL, malformed URL — under one type so the
    wizard / app entry can branch on a single exception class.
    """


# Match the three shapes ``git remote get-url`` may emit:
#
# 1. SSH:               git@github.com:owner/name(.git)?
# 2. SSH-config alias:  git@github.com-anything:owner/name(.git)?
#    (users with multiple GH accounts often configure ~/.ssh/config
#    with Host aliases like ``github.com-work`` so different repos
#    pick different SSH keys; the URL keeps the alias form.)
# 3. HTTPS:             https://github.com/owner/name(.git)?
#
# We never accept ``ssh://``-prefixed URLs because git's own ``remote
# add`` doesn't emit them by default; covering them would expand the
# regex without adding real-world coverage.  If a user needs it we can
# add later.
_SSH_RE = re.compile(
    r"^git@github\.com(?:-[A-Za-z0-9._-]+)?:"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"(?P<name>[A-Za-z0-9._-]+?)"
    r"(?:\.git)?$"
)
_HTTPS_RE = re.compile(
    r"^https?://github\.com/"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"(?P<name>[A-Za-z0-9._-]+?)"
    r"(?:\.git)?/?$"
)


def parse_remote_url(url: str) -> Tuple[str, str]:
    """Extract ``(owner, name)`` from a github.com remote URL.

    Args:
        url: The raw output of ``git remote get-url origin``, with or
            without trailing whitespace.

    Returns:
        A two-tuple of ``(owner, name)`` strings.

    Raises:
        RepoDetectionError: if the URL is empty, isn't a github.com
            URL, or doesn't match either supported shape.
    """

    cleaned = (url or "").strip()
    if not cleaned:
        raise RepoDetectionError("git remote URL is empty")

    for pattern in (_SSH_RE, _HTTPS_RE):
        match = pattern.match(cleaned)
        if match:
            owner = match.group("owner")
            name = match.group("name")
            # Strip a stray trailing ``.git`` that the regex's
            # non-greedy ``name`` group sometimes leaves attached when
            # the URL had unusual whitespace.  Cheap belt-and-braces.
            if name.endswith(".git"):
                name = name[: -len(".git")]
            return owner, name

    raise RepoDetectionError(
        f"unrecognized git remote URL (only github.com SSH/HTTPS supported): "
        f"{cleaned!r}"
    )


def _run_git(repo_root: Path, *args: str) -> Tuple[int, str, str]:
    """Run ``git -C <repo_root> <args>`` and return (rc, stdout, stderr).

    No ``shell=True``, argv list only — token-redaction stays in play
    via the standard logging filters.  Errors (git missing, repo
    invalid) are NOT raised here; the caller maps the rc/stderr to a
    structured response.
    """

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except FileNotFoundError as exc:
        # Caller wants to surface "git not on PATH" with a clear
        # message; raising RepoDetectionError keeps the contract clean.
        raise RepoDetectionError(
            "git binary not found on PATH; install git to use the agent"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RepoDetectionError(
            f"git command timed out after 10s: {' '.join(args)}"
        ) from exc

    return proc.returncode, proc.stdout or "", proc.stderr or ""


def detect_repo(repo_root: Optional[Path] = None) -> Tuple[str, str]:
    """Detect the GitHub ``owner/name`` slug for ``repo_root``.

    The two-step pattern (``rev-parse --show-toplevel`` then ``remote
    get-url``) gives us better error messages than a single ``remote
    get-url`` call: the user can tell whether the failure is "you're
    not in a git repo at all" vs "this is a git repo but has no
    ``origin``".

    Args:
        repo_root: Directory to detect from.  ``None`` (default) uses
            ``Path.cwd()`` — what the user expects when they launch
            Claude Code in a repo dir.

    Returns:
        ``(owner, name)`` two-tuple, never empty strings.

    Raises:
        RepoDetectionError: with a human-readable message describing
            which detection step failed.
    """

    root = repo_root if repo_root is not None else Path.cwd()
    root = Path(root).resolve()

    # Step 1: confirm we're inside a git repo.  We don't need the
    # toplevel value — just the success/failure signal — but rev-parse
    # is the canonical "are we in a repo?" probe.
    rc, _stdout, stderr = _run_git(root, "rev-parse", "--show-toplevel")
    if rc != 0:
        raise RepoDetectionError(
            f"{root} is not inside a git repository "
            f"(git said: {stderr.strip() or 'no message'})"
        )

    # Step 2: read the origin URL.
    rc, stdout, stderr = _run_git(root, "remote", "get-url", "origin")
    if rc != 0:
        raise RepoDetectionError(
            f"git repository at {root} has no 'origin' remote configured "
            f"(git said: {stderr.strip() or 'no remote'}). "
            "Add one with: git remote add origin <url>"
        )

    return parse_remote_url(stdout)


def config_filename_for(owner: str, name: str) -> str:
    """Build the per-repo config filename: ``<owner>__<name>.json``.

    Centralized so the wizard and the app loader agree on the exact
    convention.  The basename never contains a slash (we'd lose
    flatness) and never URL-encodes (we'd need to decode on lookup).
    """

    if "/" in owner or "/" in name:
        # Defensive: should never happen if parse_remote_url did its
        # job, but guard against a caller that built the slug by hand.
        raise ValueError(
            f"owner/name must not contain slashes: owner={owner!r} name={name!r}"
        )
    return f"{owner}__{name}.json"
