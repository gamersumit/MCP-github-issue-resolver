"""Pull-request creation tool (TRD-024).

One MCP tool — :func:`create_pr` — that opens a PR for the current
feature branch using the ``gh`` CLI when available and PyGithub as
the fallback.

Two design choices worth flagging:

* **No auto-push.**  ``create_pr`` does NOT call ``git push``.  The
  caller (the orchestrator) is expected to have invoked
  :func:`ghia.tools.git.push_branch` first.  If the head branch
  isn't on the remote yet, ``gh pr create`` errors out and we
  surface that as ``GIT_ERROR`` — the caller can retry after a
  push.  We keep the boundary clear so the same tool can be reused
  for branches that the user pushed manually.

* **Draft default tracks mode.**  In ``full`` mode (full-auto) the
  PR opens as a draft so a human still has to flip it to ready
  before merging — no autonomous-merge accidents.  In ``semi`` mode
  the user is already in the loop so the PR opens non-draft,
  ready for immediate review.  An explicit ``draft=...`` argument
  always wins.

Body handling: GitHub requires a ``Closes #N`` (or ``Fixes`` /
``Resolves``) marker to auto-close the linked issue when the PR
merges.  We append one if the caller's body doesn't already
reference the issue, so the issue queue stays in sync without
relying on caller discipline.

Satisfies REQ-018, REQ-021.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
from typing import Any, Optional

from ghia.app import GhiaApp
from ghia.errors import ErrorCode, ToolResponse, err, ok, wrap_tool
from ghia.integrations.github import GitHubClientError
from ghia.naming import pr_title
from ghia.tools.git import get_current_branch, get_default_branch
from ghia.tools.issues import _get_client

logger = logging.getLogger(__name__)

__all__ = ["create_pr"]


# Generous cap: ``gh pr create`` does a network round-trip and may
# need to wait on GitHub.  Two minutes is comfortable for the slow
# path without letting a wedged CLI hang us forever.
_GH_TIMEOUT_S = 120


def _build_close_marker_regex(issue_number: int) -> re.Pattern[str]:
    """Match ``Closes #N`` / ``Fixes #N`` / ``Resolves #N`` (any case).

    Word boundaries on ``N`` prevent ``#12`` matching ``#123`` (and
    vice-versa).  We accept all three keywords because GitHub does;
    the auto-close behaviour is identical for all of them.
    """

    return re.compile(
        rf"(?i)\b(?:closes|fixes|resolves)\s+#{issue_number}\b"
    )


def _ensure_close_marker(body: str, issue_number: int) -> str:
    """Append ``Closes #N`` if no close-marker for that issue is present.

    Idempotent: if the body already says ``Fixes #N``, we leave it
    alone — duplicating the marker would be ugly without changing
    GitHub's behaviour.
    """

    pattern = _build_close_marker_regex(issue_number)
    if pattern.search(body or ""):
        return body or ""
    suffix = f"Closes #{issue_number}"
    if not body:
        return suffix
    # Two newlines separate the appended marker from the user's
    # body so it lands as its own paragraph in GitHub's markdown
    # renderer.
    return f"{body.rstrip()}\n\n{suffix}"


def _parse_pr_url(stdout: str) -> Optional[str]:
    """Pull the last non-empty line out of ``gh pr create`` stdout.

    ``gh`` (≥ 2.0) prints the PR URL on its own line as the final
    output.  Newer versions occasionally prepend a "Creating pull
    request..." progress line, so we always take the last non-empty
    line rather than the first.
    """

    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    return lines[-1] if lines else None


def _parse_pr_number(url: Optional[str]) -> Optional[int]:
    """Extract the trailing PR number from ``.../pull/<n>``.

    Returns ``None`` if the URL doesn't look like a GitHub PR URL —
    we still return the URL string in that case, so callers see
    *something* useful.
    """

    if not url:
        return None
    match = re.search(r"/pull/(\d+)", url)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


async def _create_via_gh(
    app: GhiaApp,
    *,
    title: str,
    body: str,
    base: str,
    head: str,
    draft: bool,
) -> ToolResponse:
    """Path 1: ``gh pr create``.  Returns a structured ToolResponse."""

    argv: list[str] = [
        "gh",
        "pr",
        "create",
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

    def _call() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            cwd=str(app.repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=_GH_TIMEOUT_S,
        )

    try:
        proc = await asyncio.to_thread(_call)
    except subprocess.TimeoutExpired:
        return err(ErrorCode.NETWORK_ERROR, f"gh pr create timed out after {_GH_TIMEOUT_S}s")
    except FileNotFoundError:
        # Defensive — caller already shutil.which'd, but a race
        # could remove gh between then and now.
        return err(ErrorCode.GIT_ERROR, "gh executable not found on PATH")

    if proc.returncode != 0:
        stderr_text = (proc.stderr or "").strip()
        # ``gh`` returns "a pull request for branch ... already
        # exists" on duplicates — sniff the text and remap to the
        # right code so callers can branch cleanly.
        if "already exists" in stderr_text.lower():
            return err(ErrorCode.PR_EXISTS, stderr_text or "PR already exists")
        return err(
            ErrorCode.GIT_ERROR,
            stderr_text or f"gh pr create exited with code {proc.returncode}",
        )

    url = _parse_pr_url(proc.stdout)
    number = _parse_pr_number(url)
    return ok({
        "url": url,
        "number": number,
        "draft": draft,
        "head": head,
        "base": base,
        "body_used": body,
    })


async def _create_via_pygithub(
    app: GhiaApp,
    *,
    title: str,
    body: str,
    base: str,
    head: str,
    draft: bool,
) -> ToolResponse:
    """Path 2: PyGithub fallback.  Used when ``gh`` isn't on PATH."""

    client = _get_client(app)
    try:
        result = await client.create_pull_request(
            head=head,
            base=base,
            title=title,
            body=body,
            draft=draft,
        )
    except GitHubClientError as exc:
        # PyGithub returns 422 for duplicate PRs and our generic
        # mapper turns that into NETWORK_ERROR.  Sniff the message
        # so the structured code matches the gh-CLI path.
        msg_lower = exc.message.lower()
        if "already exists" in msg_lower or "pull request already" in msg_lower:
            return err(ErrorCode.PR_EXISTS, exc.message)
        return err(exc.code, exc.message)

    payload = {
        "url": result.get("html_url"),
        "number": result.get("number"),
        "draft": draft,
        "head": head,
        "base": base,
        "body_used": body,
    }
    return ok(payload)


@wrap_tool
async def create_pr(
    app: GhiaApp,
    *,
    issue_number: int,
    title: str,
    body: str,
    base: Optional[str] = None,
    draft: Optional[bool] = None,
) -> ToolResponse:
    """Open a PR for the current feature branch.

    Args:
        issue_number: Issue this PR resolves.  Used for both the
            default title (``Fix #N: ...``) and the ``Closes #N``
            marker enforcement in the body.
        title: PR title.  Empty string falls back to
            :func:`ghia.naming.pr_title` (``"Fix #N"``).
        body: PR body markdown.  A ``Closes #N`` line is appended
            unless the body already contains a close-marker for
            this issue.
        base: Target branch.  ``None`` → detected default branch.
        draft: ``None`` → derived from session mode (``full`` →
            draft; ``semi`` → non-draft).  An explicit value wins.

    Returns:
        ``ok({url, number, draft, head, base, body_used})`` on
        success.

    Refuses to PR from the default branch — that branch IS the
    target and a self-PR is nonsensical.
    """

    if not isinstance(issue_number, int) or issue_number <= 0:
        return err(
            ErrorCode.INVALID_INPUT,
            f"issue_number must be a positive int, got {issue_number!r}",
        )

    # Resolve draft from mode if the caller didn't pin it.  We read
    # from the live session so a mode-change mid-flight is honoured.
    if draft is None:
        state = await app.session.read()
        draft = state.mode == "full"

    # Resolve base branch.
    if base is None:
        base_resp = await get_default_branch(app)
        if not base_resp.success:
            return base_resp
        base = base_resp.data["default_branch"]

    # Resolve head branch (current).  Must not be the default —
    # otherwise we'd be PR'ing main into main.
    head_resp = await get_current_branch(app)
    if not head_resp.success:
        return head_resp
    head = head_resp.data["current_branch"]
    if head == base:
        return err(
            ErrorCode.ON_DEFAULT_BRANCH_REFUSED,
            f"create_pr requires a feature branch; current is {head!r} "
            f"which matches the base",
        )

    # Final title and body.
    final_title = title if title and title.strip() else pr_title(issue_number, "")
    final_body = _ensure_close_marker(body or "", issue_number)

    # Prefer the gh CLI; fall back to PyGithub when gh isn't on
    # PATH.  Both paths produce the same response shape.
    if shutil.which("gh") is not None:
        return await _create_via_gh(
            app,
            title=final_title,
            body=final_body,
            base=base,
            head=head,
            draft=draft,
        )
    return await _create_via_pygithub(
        app,
        title=final_title,
        body=final_body,
        base=base,
        head=head,
        draft=draft,
    )
