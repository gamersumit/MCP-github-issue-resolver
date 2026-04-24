"""Pull-request creation tool (TRD-024, v0.2 refactor).

One MCP tool — :func:`create_pr` — that opens a PR for the current
feature branch via the ``gh`` CLI integration.  v0.2 dropped the
PyGithub fallback path because the new architecture *requires* gh
(it's the auth boundary), so falling back to anything else doesn't
make sense.

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

import logging
import re
from typing import Optional

from ghia.app import GhiaApp
from ghia.errors import ErrorCode, ToolResponse, err, ok, wrap_tool
from ghia.integrations import gh_cli
from ghia.integrations.gh_cli import GhAuthError, GhUnavailable
from ghia.naming import pr_title
from ghia.tools.git import get_current_branch, get_default_branch

logger = logging.getLogger(__name__)

__all__ = ["create_pr"]


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
    """Open a PR for the current feature branch via ``gh pr create``.

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

    # Hand off to gh_cli — every error mapping (PR_EXISTS,
    # REPO_NOT_FOUND, RATE_LIMITED, …) lives there so this layer
    # only needs to translate the result shape.
    try:
        result = await gh_cli.create_pull_request(
            app.repo_full_name,
            title=final_title,
            body=final_body,
            base=base,
            head=head,
            draft=draft,
        )
    except GhUnavailable as exc:
        # The new model REQUIRES gh — there's no PyGithub fallback
        # to fall through to.  We surface this as GIT_ERROR (closest
        # existing code; the enum is closed) with an actionable
        # message so the user knows what to install.
        return err(ErrorCode.GIT_ERROR, str(exc))
    except GhAuthError as exc:
        return err(exc.code, exc.message)

    return ok({
        "url": result.get("html_url"),
        "number": result.get("number"),
        "draft": draft,
        "head": head,
        "base": base,
        "body_used": final_body,
    })
