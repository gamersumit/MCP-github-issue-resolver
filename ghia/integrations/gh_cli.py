"""GitHub CLI integration (v0.2 refactor — replaces PyGithub wrapper).

Centralizes every GitHub API call as a subprocess invocation of the
``gh`` CLI.  This is a deliberate inversion from the PyGithub-based
client: instead of holding our own credentials and talking HTTPS, we
delegate auth (and the network round-trip) to the user's already-
configured ``gh``.  Two consequences:

1. **No PAT in our config.**  ``gh`` reads its token from the OS
   keychain (or its own config tree), so the agent never sees it.
   Cross-account workflows become trivial: ``gh auth switch -u other``
   and the very next call here uses the new account.

2. **All output crosses the subprocess boundary as text.**  Every gh
   subcommand we use supports ``--json <fields>`` so we get parseable
   output without scraping; rate-limit and auth errors are sniffed
   from stderr text, which is stable enough across gh versions to be
   safe to match on.

Three concerns drive the design — same as the prior PyGithub wrapper
intentionally:

* **Token safety (REQ-023).**  The redaction filter stays installed
  defensively even though gh shouldn't leak its keychain token.
  Errors raised here build their messages from gh stdout/stderr only,
  scrubbed through ``ghia.redaction.scrub`` before they reach a
  ``ToolResponse``.

* **No blocking on the event loop.**  ``subprocess.run`` is sync;
  every public coro here off-loads via ``asyncio.to_thread`` so a
  slow ``gh`` invocation never stalls the FastMCP loop.

* **Structured errors (REQ-025).**  Every gh failure maps to a
  :class:`GhAuthError` carrying one of the canonical
  :class:`ghia.errors.ErrorCode` values.  No new error codes were
  added (per the established discipline) — the closest existing code
  is reused even when the semantics shift slightly (e.g.
  ``TOKEN_INVALID`` is the closest match for "gh isn't authenticated"
  even though no token is involved).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from ghia.errors import ErrorCode
from ghia.network import format_rate_limit_reset
from ghia.redaction import scrub

logger = logging.getLogger(__name__)

__all__ = [
    "GhUnavailable",
    "GhAuthError",
    "gh_available",
    "auth_status",
    "repo_view",
    "list_issues",
    "get_issue",
    "post_issue_comment",
    "add_label",
    "list_open_prs",
    "create_pull_request",
]


# Generous cap: ``gh`` does a network round-trip and may need to wait on
# GitHub.  Two minutes is comfortable for the slow path without letting
# a wedged CLI hang the event loop forever.
_GH_TIMEOUT_S = 120


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class GhUnavailable(Exception):
    """Raised when ``gh`` is not on PATH.

    Distinct from :class:`GhAuthError` so the wizard / setup flow can
    print install instructions specifically (rather than the generic
    "auth failed" message).
    """


@dataclass
class GhAuthError(Exception):
    """Structured error from a gh subprocess call.

    Carries a canonical :class:`ErrorCode` so the calling tool can
    convert it directly to ``err(code, message)`` without further
    translation.  ``reset_at`` is set only on rate-limit errors
    (otherwise ``None``) so callers can schedule a precise retry.

    The ``message`` field is built from gh's stdout/stderr — never from
    argv or token-bearing strings — and run through :func:`scrub`
    before storage so even an unexpected token-shaped substring can't
    leak through ``str(error)``.
    """

    code: ErrorCode
    message: str
    reset_at: Optional[datetime] = None

    def __str__(self) -> str:  # pragma: no cover — trivial
        return self.message


# ----------------------------------------------------------------------
# Subprocess plumbing
# ----------------------------------------------------------------------


def gh_available() -> bool:
    """Return True iff ``gh`` is on PATH.

    Cheap wrapper around ``shutil.which`` — the wizard uses it for an
    install-instructions check before any real call.  We deliberately
    DON'T cache the result: a user who ran the wizard, installed gh,
    then re-ran the wizard expects the second run to detect it.
    """

    return shutil.which("gh") is not None


def _run_gh_sync(argv: list[str], *, input_text: Optional[str] = None) -> subprocess.CompletedProcess[str]:
    """Synchronous gh subprocess invocation.

    Pulled out of the async helper so it can be patched in tests with
    a single ``monkeypatch.setattr(gh_cli, "_run_gh_sync", ...)`` call
    without having to wrestle ``asyncio.to_thread``.

    Argv list, no ``shell=True``.  ``input_text`` is wired through for
    completeness — the current callers all pass everything via
    ``--body``/``--title`` flags, but a future caller posting large
    bodies will want to use stdin instead of stuffing them into a
    single argv entry that may overrun ARG_MAX.
    """

    if not gh_available():
        raise GhUnavailable(
            "gh CLI is not on PATH; install from https://cli.github.com/"
        )
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        timeout=_GH_TIMEOUT_S,
        input=input_text,
    )


async def _run_gh(argv: list[str], *, input_text: Optional[str] = None) -> subprocess.CompletedProcess[str]:
    """Async wrapper — every public coro funnels through here.

    ``asyncio.to_thread`` is the right primitive: gh subprocess wait()
    is GIL-released so the event loop can keep ticking, and the call
    site stays linear (no callbacks).
    """

    return await asyncio.to_thread(_run_gh_sync, argv, input_text=input_text)


# ----------------------------------------------------------------------
# Error classification
# ----------------------------------------------------------------------


_RATE_LIMIT_RE = re.compile(r"API rate limit exceeded", re.IGNORECASE)
_RESET_AT_RE = re.compile(r"X-RateLimit-Reset[:\s]+(\d+)", re.IGNORECASE)
_RESET_HUMAN_RE = re.compile(
    r"resets at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z?)", re.IGNORECASE
)


def _parse_rate_limit_reset(stderr: str) -> Optional[datetime]:
    """Pull ``X-RateLimit-Reset`` from gh stderr if present.

    gh prints rate-limit headers when ``GH_DEBUG=api`` is set but also
    sometimes echoes a "rate limited until ..." line in plain runs.
    We try both forms; failure is non-fatal — the caller still gets
    ``RATE_LIMITED`` with an informative-only ``reset_at=None``.
    """

    # Numeric epoch (debug mode).
    m = _RESET_AT_RE.search(stderr)
    if m:
        try:
            return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            return None
    # Human ISO form.
    m = _RESET_HUMAN_RE.search(stderr)
    if m:
        try:
            iso = m.group(1).rstrip("Z")
            dt = datetime.fromisoformat(iso)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _classify_error(stderr: str, stdout: str = "") -> GhAuthError:
    """Map gh failure output to a structured :class:`GhAuthError`.

    The string-matching is case-insensitive and intentionally
    conservative: when in doubt, we fall through to ``INVALID_INPUT``
    with the raw stderr so the user can see what gh actually said.

    ErrorCode mapping:
    * "could not resolve to a Repository" / HTTP 404 → ``REPO_NOT_FOUND``
    * "authentication required" / "could not resolve auth" / HTTP 401 →
      ``TOKEN_INVALID`` (semantic name is wrong now — there's no token —
      but the enum is closed and this is the closest existing match)
    * "API rate limit exceeded" → ``RATE_LIMITED`` with parsed reset_at
    * "already exists" (PR creation) → ``PR_EXISTS``
    * Network/dns/timeout hints → ``NETWORK_ERROR``
    * Anything else → ``INVALID_INPUT``
    """

    # Combine for matching but keep stderr as the primary source for
    # the message (it's where gh writes user-facing errors).
    combined = f"{stderr}\n{stdout}".lower()
    raw_message = (stderr or stdout or "gh command failed").strip()
    safe_message = scrub(raw_message)

    # Rate limit takes precedence over generic auth so a 403 caused by
    # quota exhaustion doesn't get mis-tagged as TOKEN_INVALID.
    if _RATE_LIMIT_RE.search(combined):
        reset_at = _parse_rate_limit_reset(stderr)
        if reset_at is not None:
            detail = format_rate_limit_reset(int(reset_at.timestamp()))
            human = f"GitHub rate limit exceeded; {detail}."
        else:
            # Pin the wording so downstream UIs / tests can key off
            # the "reset time unavailable" suffix as a stable signal.
            human = "GitHub rate limit exceeded; reset time unavailable."
        return GhAuthError(
            code=ErrorCode.RATE_LIMITED,
            message=human,
            reset_at=reset_at,
        )

    if (
        "could not resolve to a repository" in combined
        or "http 404" in combined
        or "404 not found" in combined
    ):
        return GhAuthError(
            code=ErrorCode.REPO_NOT_FOUND,
            message=f"GitHub returned 404: {safe_message}",
        )

    if (
        "authentication required" in combined
        or "could not resolve auth" in combined
        or "you are not logged into" in combined
        or "http 401" in combined
        or "bad credentials" in combined
    ):
        return GhAuthError(
            code=ErrorCode.TOKEN_INVALID,
            message=f"gh authentication failed: {safe_message}",
        )

    if "already exists" in combined:
        return GhAuthError(
            code=ErrorCode.PR_EXISTS,
            message=safe_message or "PR already exists",
        )

    # Sniff for network-class failures.  We deliberately do NOT use
    # `ghia.network.classify_network_error` here — that helper is for
    # transport exceptions, but gh subprocess failures surface via
    # text on stderr.  String matching is what we have.
    if any(
        token in combined
        for token in (
            "could not resolve host",
            "connection refused",
            "no such host",
            "network is unreachable",
            "timeout",
            "timed out",
            "tls handshake",
            "dns",
        )
    ):
        return GhAuthError(
            code=ErrorCode.NETWORK_ERROR,
            message=f"network error from gh: {safe_message}",
        )

    return GhAuthError(
        code=ErrorCode.INVALID_INPUT,
        message=safe_message or "gh command failed with no output",
    )


def _ensure_ok(proc: subprocess.CompletedProcess[str]) -> None:
    """Raise :class:`GhAuthError` if ``proc.returncode != 0``."""

    if proc.returncode != 0:
        raise _classify_error(proc.stderr or "", proc.stdout or "")


def _parse_json(stdout: str, *, context: str) -> Any:
    """``json.loads`` with a structured error wrapper.

    Centralizes the "gh said success but emitted garbage" failure
    path so each caller doesn't repeat the boilerplate.  The error
    carries ``INVALID_INPUT`` because the only realistic cause is a
    gh version mismatch (a new field shape, a stripped --json flag),
    which is a setup problem rather than a transport one.
    """

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise GhAuthError(
            code=ErrorCode.INVALID_INPUT,
            message=f"could not parse gh output ({context}): {exc.msg}",
        ) from exc


# ----------------------------------------------------------------------
# Result shape normalization
# ----------------------------------------------------------------------
#
# gh's --json output uses camelCase field names (nameWithOwner,
# createdAt) while the rest of the codebase has historically consumed
# the snake_case shape PyGithub returned (html_url, created_at,
# comments_count).  We normalize at this boundary so the tools layer
# doesn't have to care about the rename.


def _normalize_user(value: Any) -> Optional[str]:
    """Pull a ``login`` string off gh's user-shaped object."""

    if isinstance(value, dict):
        login = value.get("login")
        if isinstance(login, str):
            return login
    if isinstance(value, str):
        return value
    return None


def _normalize_assignees(value: Any) -> list[str]:
    """gh emits ``assignees`` as a list of {login: ...} dicts."""

    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        login = _normalize_user(item)
        if login:
            out.append(login)
    return out


def _normalize_labels(value: Any) -> list[str]:
    """gh emits ``labels`` as a list of {name: ..., color: ...} dicts."""

    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            name = item.get("name")
            if isinstance(name, str):
                out.append(name)
        elif isinstance(item, str):
            out.append(item)
    return out


def _normalize_comments_count(value: Any) -> int:
    """gh emits ``comments`` as a list of comment dicts.

    The historical PyGithub shape had ``comments_count`` as an int
    (the count, not the list).  We preserve that contract by
    returning ``len(value)`` when value is a list, otherwise the
    int-cast (gh sometimes returns a number on terse calls).
    """

    if isinstance(value, list):
        return len(value)
    if isinstance(value, int):
        return value
    return 0


def _normalize_issue(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert one gh issue dict to the agent's canonical shape."""

    return {
        "number": raw.get("number"),
        "title": raw.get("title") or "",
        "body": raw.get("body") or "",
        "labels": _normalize_labels(raw.get("labels")),
        "html_url": raw.get("url"),
        "created_at": raw.get("createdAt"),
        "updated_at": raw.get("updatedAt"),
        "author": _normalize_user(raw.get("author")),
        "assignees": _normalize_assignees(raw.get("assignees")),
        "comments_count": _normalize_comments_count(raw.get("comments")),
    }


def _normalize_pr(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert one gh PR dict to the agent's canonical shape."""

    return {
        "number": raw.get("number"),
        "title": raw.get("title") or "",
        "body": raw.get("body") or "",
        "html_url": raw.get("url"),
        "head_ref": raw.get("headRefName"),
    }


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


# The standard --json field set we want for issue listings.  Pinned in
# one place so a future addition (assignees got renamed?) is a single
# edit, not a search-and-replace.
_ISSUE_JSON_FIELDS = (
    "number,title,body,labels,url,createdAt,updatedAt,author,assignees,comments"
)
_PR_JSON_FIELDS = "number,title,body,url,headRefName"


async def auth_status() -> dict[str, Any]:
    """Return ``{authenticated, active_account, hostname}``.

    Parses ``gh auth status --hostname github.com`` text output.  gh
    doesn't expose a JSON variant for auth-status, so we live with the
    text format — which is stable enough across versions to match on.

    Output shape we look for (gh ≥ 2.0):

        github.com
          ✓ Logged in to github.com account <login> (...)
          - Active account: true
          ...

    When multiple accounts are configured, only the one tagged
    ``Active account: true`` is returned.  When no account is active,
    ``authenticated`` is False and ``active_account`` is None.
    """

    proc = await _run_gh(["gh", "auth", "status", "--hostname", "github.com"])
    # ``gh auth status`` returns rc=1 when not authenticated, so we
    # don't blanket-_ensure_ok here — the unauth case is a normal
    # answer to the question, not an error to raise on.
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")

    # The "Active account" marker is gh's way of disambiguating in
    # multi-account setups; when only one account is configured, gh
    # still emits a single block but doesn't always tag it active.
    # We pick the active block first, then fall back to any logged-in
    # block, which matches user expectations for both layouts.
    blocks = _split_account_blocks(text)
    active = _pick_active_block(blocks) or _pick_first_logged_in_block(blocks)

    if active is None:
        return {
            "authenticated": False,
            "active_account": None,
            "hostname": "github.com",
        }

    return {
        "authenticated": True,
        "active_account": active,
        "hostname": "github.com",
    }


def _split_account_blocks(text: str) -> list[str]:
    """Split gh auth status text into per-account chunks.

    A "block" starts at a "Logged in to" line and runs until the next
    one (or end-of-text).  This is the cleanest way to associate
    "Active account: true" with the right login when multiple accounts
    appear under a single hostname header.
    """

    lines = text.splitlines()
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if "Logged in to" in line:
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)
    return ["\n".join(b) for b in blocks]


_ACCOUNT_RE = re.compile(r"account\s+([A-Za-z0-9][A-Za-z0-9-]*)")


def _pick_active_block(blocks: list[str]) -> Optional[str]:
    """Find the block tagged ``Active account: true`` and return its login."""

    for block in blocks:
        if re.search(r"Active account:\s*true", block, re.IGNORECASE):
            m = _ACCOUNT_RE.search(block)
            if m:
                return m.group(1)
    return None


def _pick_first_logged_in_block(blocks: list[str]) -> Optional[str]:
    """Return the first block's login as a fallback for single-account setups."""

    for block in blocks:
        m = _ACCOUNT_RE.search(block)
        if m:
            return m.group(1)
    return None


async def repo_view(repo: str) -> dict[str, Any]:
    """Verify the active account can see ``repo`` and return basic info.

    Returns a dict with at least ``name``, ``nameWithOwner``,
    ``viewerPermission``, ``defaultBranchRef`` keys (matching gh's
    --json output verbatim — this is the one place we expose the raw
    gh shape because the wizard wants to display ``viewerPermission``
    and ``defaultBranchRef.name`` directly).
    """

    proc = await _run_gh([
        "gh",
        "repo",
        "view",
        repo,
        "--json",
        "name,nameWithOwner,viewerPermission,defaultBranchRef",
    ])
    _ensure_ok(proc)
    return _parse_json(proc.stdout, context="repo view")


async def list_issues(
    repo: str,
    *,
    label: Optional[str] = None,
    state: str = "open",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return open issues, optionally filtered by label.

    The shape of each dict matches the historical PyGithub-derived
    contract — keys: ``number, title, body, labels, html_url,
    created_at, updated_at, author, assignees, comments_count``.

    ``state`` accepts gh's set: ``open|closed|all``.  ``limit`` caps
    the result set; gh defaults to 30, we default to 100 because the
    polling tick wants the full picker queue, not a paged slice.
    """

    argv = [
        "gh",
        "issue",
        "list",
        "--repo",
        repo,
        "--json",
        _ISSUE_JSON_FIELDS,
        "--state",
        state,
        "--limit",
        str(limit),
    ]
    if label:
        argv.extend(["--label", label])

    proc = await _run_gh(argv)
    _ensure_ok(proc)
    raw = _parse_json(proc.stdout, context="issue list")
    if not isinstance(raw, list):
        raise GhAuthError(
            code=ErrorCode.INVALID_INPUT,
            message="gh issue list returned non-list JSON",
        )
    return [_normalize_issue(item) for item in raw if isinstance(item, dict)]


async def get_issue(repo: str, number: int) -> dict[str, Any]:
    """Return a single issue by number, normalized to the agent's shape."""

    proc = await _run_gh([
        "gh",
        "issue",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        _ISSUE_JSON_FIELDS,
    ])
    _ensure_ok(proc)
    raw = _parse_json(proc.stdout, context="issue view")
    if not isinstance(raw, dict):
        raise GhAuthError(
            code=ErrorCode.INVALID_INPUT,
            message="gh issue view returned non-dict JSON",
        )
    return _normalize_issue(raw)


async def post_issue_comment(
    repo: str, number: int, body: str
) -> dict[str, Any]:
    """Post a comment on an issue.

    gh prints the comment URL on stdout and exits 0; there's no
    --json flag for this subcommand, so we fabricate the historical
    response shape: ``{html_url, created_at}``.  The caller never
    used the comment's ``id`` so we don't try to recover it.
    """

    proc = await _run_gh([
        "gh",
        "issue",
        "comment",
        str(number),
        "--repo",
        repo,
        "--body",
        body,
    ])
    _ensure_ok(proc)
    # Last non-empty line of stdout is the comment URL — same parsing
    # convention as ``gh pr create``.
    url = _last_nonempty_line(proc.stdout)
    return {
        "html_url": url,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }


async def add_label(repo: str, number: int, label: str) -> None:
    """Add a label to an issue.  No return — used by retry / fixup paths."""

    proc = await _run_gh([
        "gh",
        "issue",
        "edit",
        str(number),
        "--repo",
        repo,
        "--add-label",
        label,
    ])
    _ensure_ok(proc)


async def list_open_prs(repo: str) -> list[dict[str, Any]]:
    """Return all open PRs as dicts: ``{number, title, body, html_url, head_ref}``."""

    proc = await _run_gh([
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--json",
        _PR_JSON_FIELDS,
        "--state",
        "open",
        "--limit",
        "100",
    ])
    _ensure_ok(proc)
    raw = _parse_json(proc.stdout, context="pr list")
    if not isinstance(raw, list):
        raise GhAuthError(
            code=ErrorCode.INVALID_INPUT,
            message="gh pr list returned non-list JSON",
        )
    return [_normalize_pr(item) for item in raw if isinstance(item, dict)]


async def create_pull_request(
    repo: str,
    *,
    title: str,
    body: str,
    base: str,
    head: str,
    draft: bool,
) -> dict[str, Any]:
    """Open a PR.  Returns ``{number, html_url, draft, head, base}``.

    Mirrors the prior PyGithub wrapper's signature so the tools layer
    didn't have to change its keyword names when we ripped PyGithub
    out.  The PR number is parsed from the URL gh prints because the
    ``pr create`` subcommand has no ``--json`` flag.
    """

    argv = [
        "gh",
        "pr",
        "create",
        "--repo",
        repo,
        "--title",
        title,
        "--body",
        body,
        "--base",
        base,
        "--head",
        head,
    ]
    if draft:
        argv.append("--draft")

    proc = await _run_gh(argv)
    _ensure_ok(proc)
    url = _last_nonempty_line(proc.stdout)
    number = _parse_pr_number(url)
    return {
        "number": number,
        "html_url": url,
        "draft": draft,
        "head": head,
        "base": base,
    }


# ----------------------------------------------------------------------
# Small parsing helpers (shared by multiple callers)
# ----------------------------------------------------------------------


def _last_nonempty_line(stdout: str) -> Optional[str]:
    """Return the last non-empty line of ``stdout``, stripped.

    gh ≥ 2.0 prints status spinners then a final URL on its own line;
    we always want the URL, never the spinner.  Returning None when
    stdout is empty keeps callers from raising on a silent success.
    """

    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    return lines[-1] if lines else None


def _parse_pr_number(url: Optional[str]) -> Optional[int]:
    """Extract the trailing PR number from ``.../pull/<n>``."""

    if not url:
        return None
    m = re.search(r"/pull/(\d+)", url)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None
