"""Undo / rollback tool (TRD-028).

One MCP tool — :func:`undo_last_change` — that hard-resets HEAD by
one commit *if* it can verify the agent authored the commit and we
are not standing on the protected default branch.

Two refusal codes, distinct on purpose so callers can distinguish
the failure mode:

* ``UNDO_REFUSED_PROTECTED_BRANCH`` — current branch IS the default
  branch.  We never touch the default branch from this tool, full
  stop, regardless of authorship.
* ``UNDO_REFUSED_NOT_OURS`` — HEAD's author email doesn't match the
  ``user.email`` from ``git config``.  We use the user's git
  identity as the agent identity because :class:`Config` doesn't
  carry an explicit ``agent_email`` field, and the wizard
  configures git's own user identity at install time.

The reset is a hard reset (``git reset --hard HEAD~1``) — the work
genuinely goes away.  Callers that want a softer rollback should
use ``git revert`` from a commit handler instead.

Satisfies REQ-019.
"""

from __future__ import annotations

import logging
from typing import Optional

from ghia.app import GhiaApp
from ghia.errors import ErrorCode, ToolResponse, err, ok, wrap_tool
from ghia.tools.git import (
    _git_error,
    _try_run_git,
    get_current_branch,
    get_default_branch,
)

logger = logging.getLogger(__name__)

__all__ = ["undo_last_change"]


async def _read_head_author_email(
    app: GhiaApp,
) -> tuple[Optional[str], Optional[ToolResponse]]:
    """Return HEAD's author email or a structured error response."""

    rc, out, errout, err_resp = await _try_run_git(
        app, "log", "-1", "--format=%ae"
    )
    if err_resp is not None:
        return None, err_resp
    if rc != 0:
        return None, _git_error(("log", "-1", "--format=%ae"), rc, errout)
    return out.strip(), None


async def _read_configured_user_email(
    app: GhiaApp,
) -> tuple[Optional[str], Optional[ToolResponse]]:
    """Return ``git config user.email`` or a structured error response.

    A missing user.email (rc != 0) returns ``INVALID_INPUT`` rather
    than a git error: the operator hasn't told git who they are,
    and that's a configuration omission they need to fix before we
    can verify authorship.
    """

    rc, out, _, err_resp = await _try_run_git(
        app, "config", "user.email"
    )
    if err_resp is not None:
        return None, err_resp
    if rc != 0:
        return None, err(
            ErrorCode.INVALID_INPUT,
            "git user.email is not configured; set it before using undo",
        )
    return out.strip(), None


@wrap_tool
async def undo_last_change(app: GhiaApp) -> ToolResponse:
    """Hard-reset HEAD~1 if we authored the commit and aren't on default.

    Returns ``{undone_sha, new_head_sha}`` on success.  Callers can
    surface ``undone_sha`` if they want to give the user a "to undo
    the undo, ``git reset --hard <sha>``" hint.
    """

    # Default-branch guard runs first — even if the commit is ours,
    # we never mutate the default branch from this tool.
    default_resp = await get_default_branch(app)
    if not default_resp.success:
        return default_resp
    default_branch = default_resp.data["default_branch"]

    current_resp = await get_current_branch(app)
    if not current_resp.success:
        return current_resp
    current_branch = current_resp.data["current_branch"]

    if current_branch == default_branch:
        return err(
            ErrorCode.UNDO_REFUSED_PROTECTED_BRANCH,
            f"refusing to undo on protected default branch {default_branch!r}",
        )

    # Authorship check: HEAD must have been authored by the
    # configured agent identity (== git's user.email).  This guards
    # against undoing a human's commit that landed in front of the
    # agent's work.
    head_email, err_resp = await _read_head_author_email(app)
    if err_resp is not None:
        return err_resp
    agent_email, err_resp = await _read_configured_user_email(app)
    if err_resp is not None:
        return err_resp

    if not head_email or head_email != agent_email:
        return err(
            ErrorCode.UNDO_REFUSED_NOT_OURS,
            f"HEAD was not authored by the agent identity "
            f"(head={head_email!r}, agent={agent_email!r})",
        )

    # Capture the SHA we're about to undo before we destroy it.
    rc, undone_sha_out, errout, err_resp = await _try_run_git(
        app, "rev-parse", "HEAD"
    )
    if err_resp is not None:
        return err_resp
    if rc != 0:
        return _git_error(("rev-parse", "HEAD"), rc, errout)
    undone_sha = undone_sha_out.strip()

    # Hard reset.  This is the destructive op — by this point all
    # guards have passed and we're committing to the rollback.
    rc, _, errout, err_resp = await _try_run_git(
        app, "reset", "--hard", "HEAD~1"
    )
    if err_resp is not None:
        return err_resp
    if rc != 0:
        return _git_error(("reset", "--hard", "HEAD~1"), rc, errout)

    rc, new_head_out, errout, err_resp = await _try_run_git(
        app, "rev-parse", "HEAD"
    )
    if err_resp is not None:
        return err_resp
    if rc != 0:
        return _git_error(("rev-parse", "HEAD"), rc, errout)

    return ok({
        "undone_sha": undone_sha,
        "new_head_sha": new_head_out.strip(),
    })
