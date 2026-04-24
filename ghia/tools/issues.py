"""Issue-related MCP tools (TRD-016 + TRD-017).

Five user-visible tools that read and mutate GitHub issues plus a
companion duplicate-PR detector:

* ``list_issues`` — fetch open issues, optionally filtered by label,
  with derived priority metadata [REQ-010]
* ``get_issue`` — fetch a single issue by number [REQ-010]
* ``pick_issue`` — append an issue number to the session queue
* ``skip_issue`` — record an issue as skipped and remove from queue
* ``post_issue_comment`` — post a progress comment on an issue [REQ-013]
* ``check_issue_has_open_pr`` — non-mutating duplicate detector that
  combines a GitHub-PR signal with a local-branch signal and reports
  *both* without ever auto-skipping [REQ-013b]

Every entry point is wrapped with :func:`ghia.errors.wrap_tool` so a
:class:`ghia.integrations.github.GitHubClientError` (or any other
exception) becomes a structured ``ToolResponse(err=...)`` rather than
reaching the MCP transport.

The :class:`GitHubClient` is built lazily and cached on the
:class:`GhiaApp` so a tool call doesn't pay the construction cost
every time, but a config change (new repo or new token) is detected
and triggers a rebuild on the next call.
"""

from __future__ import annotations

import logging
import re
import subprocess
from typing import Any, Optional

from ghia.app import GhiaApp
from ghia.errors import ErrorCode, ToolResponse, err, ok, wrap_tool
from ghia.integrations.github import GitHubClient, GitHubClientError

logger = logging.getLogger(__name__)

__all__ = [
    "list_issues",
    "get_issue",
    "pick_issue",
    "skip_issue",
    "post_issue_comment",
    "check_issue_has_open_pr",
]


# ----------------------------------------------------------------------
# Priority derivation
# ----------------------------------------------------------------------

# Label substrings (matched case-insensitively) that bump an issue to
# the corresponding bucket.  Order doesn't matter — first match wins
# in priority order: high beats normal beats low, with "normal" as the
# default when no rule fires.
_HIGH_LABEL_TOKENS: tuple[str, ...] = (
    "priority/high",
    "priority:high",
    "priority-high",
    "bug",
)
_NORMAL_LABEL_TOKENS: tuple[str, ...] = (
    "enhancement",
    "feature",
)
_LOW_LABEL_TOKENS: tuple[str, ...] = (
    "documentation",
    "docs",
    "chore",
)


def _derive_priority(labels: list[str]) -> str:
    """Return ``"high" | "normal" | "low"`` based on issue labels.

    Case-insensitive substring match on each label name.  ``"high"``
    wins over ``"normal"`` wins over ``"low"`` so a "bug" labelled
    "documentation" is still classified as a bug.
    """

    lowered = [str(label).lower() for label in labels]

    def any_match(tokens: tuple[str, ...]) -> bool:
        return any(token in lab for lab in lowered for token in tokens)

    if any_match(_HIGH_LABEL_TOKENS):
        return "high"
    if any_match(_LOW_LABEL_TOKENS) and not any_match(_NORMAL_LABEL_TOKENS):
        # Low-only labels (docs/chore without enhancement/feature) -> low.
        return "low"
    if any_match(_NORMAL_LABEL_TOKENS):
        return "normal"
    if any_match(_LOW_LABEL_TOKENS):
        return "low"
    return "normal"


def _annotate(issue: dict[str, Any]) -> dict[str, Any]:
    """Add the derived ``priority`` field to an issue dict (in place safe).

    We work on a shallow copy so callers that hold the original dict
    aren't mutated as a side-effect of this enrichment.
    """

    enriched = dict(issue)
    enriched["priority"] = _derive_priority(
        list(issue.get("labels", []) or [])
    )
    return enriched


# ----------------------------------------------------------------------
# Lazy client accessor
# ----------------------------------------------------------------------


def _get_client(app: GhiaApp) -> GitHubClient:
    """Return a cached :class:`GitHubClient` matching the current config.

    We stash the client on a private attribute of ``app`` (the dataclass
    permits arbitrary attribute assignment because it isn't ``frozen``).
    The cache key is ``(token, repo)`` so a config swap mid-session
    produces a fresh client on the very next call.
    """

    cached: Optional[GitHubClient] = getattr(app, "_github_client", None)
    cache_key: Optional[tuple[str, str]] = getattr(
        app, "_github_client_key", None
    )
    current_key = (app.config.token, app.config.repo)

    if cached is not None and cache_key == current_key:
        return cached

    client = GitHubClient(token=app.config.token, repo_full_name=app.config.repo)
    setattr(app, "_github_client", client)
    setattr(app, "_github_client_key", current_key)
    return client


# ----------------------------------------------------------------------
# Read tools
# ----------------------------------------------------------------------


@wrap_tool
async def list_issues(
    app: GhiaApp, label: Optional[str] = None
) -> ToolResponse:
    """Return open issues, optionally filtered by label.

    When ``label`` is ``None`` we default to ``app.config.label`` so
    the common case ("show me my queue") doesn't require the caller to
    re-pass the configured label every time.  Pass an empty string to
    get *all* open issues regardless of label.
    """

    effective_label: Optional[str]
    if label is None:
        effective_label = app.config.label
    elif label == "":
        effective_label = None
    else:
        effective_label = label

    client = _get_client(app)
    try:
        raw_issues = await client.list_issues(label=effective_label)
    except GitHubClientError as exc:
        return err(exc.code, exc.message)

    enriched = [_annotate(i) for i in raw_issues]
    return ok({"issues": enriched, "count": len(enriched)})


@wrap_tool
async def get_issue(app: GhiaApp, number: int) -> ToolResponse:
    """Fetch a single issue by number, with derived priority."""

    client = _get_client(app)
    try:
        raw = await client.get_issue(number=number)
    except GitHubClientError as exc:
        return err(exc.code, exc.message)
    return ok(_annotate(raw))


# ----------------------------------------------------------------------
# Queue mutation
# ----------------------------------------------------------------------


@wrap_tool
async def pick_issue(app: GhiaApp, number: int) -> ToolResponse:
    """Append ``number`` to the session queue (no duplicates).

    All mutations happen inside the SessionStore lock so a concurrent
    ``skip_issue`` (or any other writer) sees a consistent queue.
    Adding a number that's already in the queue is a silent no-op —
    the user expressed intent to work on it, and we don't punish them
    for re-clicking.
    """

    async with app.session.lock:
        current = await app.session.read()
        queue = list(current.queue)
        if number not in queue:
            queue.append(number)
        new_state = current.model_copy(update={"queue": queue})
        app.session._persist(new_state)

    return ok({"queue": queue})


@wrap_tool
async def skip_issue(app: GhiaApp, number: int) -> ToolResponse:
    """Record ``number`` as skipped and remove it from the queue.

    Skipping an issue that isn't in the queue is allowed — the caller
    may be skipping a number it pulled from somewhere other than the
    queue (e.g. the picker UI showing all open issues).  The skipped
    list deduplicates so repeated skips of the same number don't
    inflate the count.
    """

    async with app.session.lock:
        current = await app.session.read()
        queue = [n for n in current.queue if n != number]
        skipped = list(current.skipped)
        if number not in skipped:
            skipped.append(number)
        new_state = current.model_copy(
            update={"queue": queue, "skipped": skipped}
        )
        app.session._persist(new_state)

    return ok({"queue": queue, "skipped": skipped})


# ----------------------------------------------------------------------
# Comment posting
# ----------------------------------------------------------------------


@wrap_tool
async def post_issue_comment(
    app: GhiaApp, number: int, body: str
) -> ToolResponse:
    """Post a progress comment on an issue."""

    if not isinstance(body, str) or not body.strip():
        return err(
            ErrorCode.INVALID_INPUT,
            "comment body must be a non-empty string",
        )

    client = _get_client(app)
    try:
        result = await client.post_issue_comment(number=number, body=body)
    except GitHubClientError as exc:
        return err(exc.code, exc.message)
    return ok(result)


# ----------------------------------------------------------------------
# Duplicate-PR detection
# ----------------------------------------------------------------------


def _build_issue_reference_pattern(number: int) -> re.Pattern[str]:
    """Compile a case-insensitive regex matching common issue refs.

    Matches ``#N``, ``Closes #N``, ``Fixes #N``, ``Resolves #N`` (any
    case).  Word boundaries on the number prevent ``#12`` matching
    ``#123``.
    """

    return re.compile(rf"(?i)(?:closes|fixes|resolves)?\s*#{number}\b")


def _scan_prs_for_issue(
    prs: list[dict[str, Any]], number: int
) -> list[dict[str, Any]]:
    """Return signal entries for any PR whose title/body mentions the issue."""

    pattern = _build_issue_reference_pattern(number)
    signals: list[dict[str, Any]] = []
    for pr in prs:
        haystack = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
        if pattern.search(haystack):
            signals.append({
                "type": "pr",
                "pr_number": pr.get("number"),
                "url": pr.get("html_url"),
            })
    return signals


def _scan_local_branches(repo_root: Any, number: int) -> list[dict[str, Any]]:
    """Probe ``git branch --list`` for ``fix-issue-N*`` and ``issue-N*``.

    Subprocess errors (git missing, repo not initialised, etc.) are
    swallowed — TRD-017 is explicit that this is a best-effort signal
    and a missing git binary must NEVER cause the tool to fail.  We
    log the failure at DEBUG so operators can still investigate.
    """

    patterns = [f"fix-issue-{number}*", f"issue-{number}*"]
    try:
        completed = subprocess.run(
            ["git", "branch", "--list", *patterns],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        logger.debug("git branch probe failed: %s", type(exc).__name__)
        return []

    if completed.returncode != 0:
        # Not in a git repo, or git refused — treat as "no branches".
        return []

    signals: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        # ``git branch --list`` prints "  branch-name" or "* branch-name".
        name = line.lstrip("*").strip()
        if name:
            signals.append({"type": "branch", "name": name})
    return signals


@wrap_tool
async def check_issue_has_open_pr(
    app: GhiaApp, number: int
) -> ToolResponse:
    """Report whether an open PR or local branch already targets an issue.

    Combines two independent signals:

    * **PR signal** — open PRs on the configured repo whose title or
      body references the issue (``#N``, ``Closes #N``, ``Fixes #N``,
      ``Resolves #N``).
    * **Local branch signal** — local git branches matching the
      ``fix-issue-{N}*`` / ``issue-{N}*`` naming patterns.

    The tool never auto-skips: it returns ``has_duplicate`` and the
    raw signal list, leaving the decision to the caller (or the user).
    Subprocess failures during the local probe are treated as "no
    branch signal" — they must not crash the detector.
    """

    import asyncio

    client = _get_client(app)
    try:
        prs = await client.list_open_prs()
    except GitHubClientError as exc:
        return err(exc.code, exc.message)

    pr_signals = _scan_prs_for_issue(prs, number)

    # ``subprocess.run`` is synchronous; off-load it so we don't block
    # the event loop on a slow disk / fork.
    branch_signals = await asyncio.to_thread(
        _scan_local_branches, app.repo_root, number
    )

    signals = [*pr_signals, *branch_signals]
    return ok({
        "has_duplicate": bool(signals),
        "signals": signals,
    })
