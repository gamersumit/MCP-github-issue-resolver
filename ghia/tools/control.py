"""Control-plane tools (TRD-012).

Five user-visible MCP tools that govern the agent's lifecycle:

* ``issue_agent_start`` — transition to active, discover conventions,
  render protocol [REQ-005]
* ``issue_agent_stop``  — pause the agent [REQ-007]
* ``issue_agent_status``— read-only snapshot of the session [REQ-008]
* ``issue_agent_set_mode`` — swap semi/full mid-session [REQ-009]
* ``issue_agent_fetch_now`` — STUB until Cluster 4 lands [REQ-005]

Each function is wrapped with :func:`ghia.errors.wrap_tool` so any
stray exception becomes a structured ``ToolResponse(err=...)`` instead
of reaching the MCP transport.

All mutators read the SessionState *inside* the store lock so a
``set_mode`` call racing with a ``start`` call still produces a
consistent outcome.  ``status`` does a plain (lock-free) read — its
whole purpose is to be cheap and side-effect free.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ghia.app import GhiaApp
from ghia.convention_scan import discover_conventions
from ghia.errors import ErrorCode, ToolResponse, err, ok, wrap_tool
from ghia.protocol import format_queue_summary, render_protocol
from ghia.session import SessionState

logger = logging.getLogger(__name__)

__all__ = [
    "issue_agent_start",
    "issue_agent_stop",
    "issue_agent_status",
    "issue_agent_set_mode",
    "issue_agent_fetch_now",
]


# TRD-023 will replace this with a real ``get_default_branch`` call that
# hits the GitHub API and caches the result on the session.  Until then
# the protocol banner shows "main" and a comment flags the stub so
# reviewers remember to swap it out.
_DEFAULT_BRANCH_STUB = "main"

_VALID_MODES: frozenset[str] = frozenset({"semi", "full"})


def _iso_now() -> datetime:
    """UTC wall-clock "now" — factored out so tests can patch if ever needed."""

    return datetime.now(tz=timezone.utc)


def _human_timestamp(dt: datetime) -> str:
    """Human-readable timestamp for the protocol banner."""

    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _snapshot_dict(state: SessionState) -> dict[str, Any]:
    """``SessionState`` -> dict suitable for a ToolResponse payload.

    ``model_dump(mode="json")`` normalizes datetime fields to ISO-8601
    strings so the result serializes cleanly through MCP without a
    custom encoder.
    """

    return state.model_dump(mode="json")


# ----------------------------------------------------------------------
# start
# ----------------------------------------------------------------------


@wrap_tool
async def issue_agent_start(app: GhiaApp) -> ToolResponse:
    """Transition the agent from ``idle`` to ``active``.

    Flow:
      1. Lock the session.  Reject with ``INVALID_INPUT`` if already
         active (double-start is a user error, not a silent retry).
      2. Run convention discovery (async, thread-off-loaded).
      3. Persist: ``status="active"``, ``session_started=<now>``,
         ``discovered_conventions=<summary>``, ``default_branch=<stub>``,
         ``repo=<config.repo>``.
      4. Render the protocol string with the current mode, queue, and
         discovered conventions.
      5. Return ``{protocol, mode, queue, discovered_conventions_preview}``.

    Queue population is NOT performed here — :func:`pick_issues` in
    Cluster 4 will fill it.  This function only echoes whatever is
    already persisted so a user can call ``start`` after ``pick_issues``
    in either order.
    """

    async with app.session.lock:
        current = await app.session.read()
        if current.status == "active":
            return err(
                ErrorCode.INVALID_INPUT,
                "agent already active; call issue_agent_stop first or issue_agent_status to inspect",
            )

        # Run discovery *inside* the lock so a concurrent ``stop`` can't
        # clobber the conventions we're about to persist.  Discovery is
        # bounded (a handful of small files) so this is safe.
        discovered = await discover_conventions(app.repo_root)

        now = _iso_now()
        # TRD-023: replace _DEFAULT_BRANCH_STUB with detected branch.
        new_state = SessionState.model_validate({
            **current.model_dump(),
            "status": "active",
            "mode": current.mode,  # preserve any prior set_mode
            "repo": app.config.repo,
            "session_started": now,
            "discovered_conventions": discovered,
            "default_branch": _DEFAULT_BRANCH_STUB,
        })
        app.session._persist(new_state)

    # Render OUTSIDE the lock — it's a pure CPU op and we're done
    # mutating state.
    queue_summary = format_queue_summary(new_state.queue)
    protocol = render_protocol(
        repo=new_state.repo or app.config.repo,
        mode=new_state.mode,
        default_branch=new_state.default_branch or _DEFAULT_BRANCH_STUB,
        discovered_conventions=discovered,
        queue_summary=queue_summary,
        timestamp=_human_timestamp(now),
    )

    preview = (discovered or "")[:200]
    return ok({
        "protocol": protocol,
        "mode": new_state.mode,
        "queue": list(new_state.queue),
        "discovered_conventions_preview": preview,
        "session_started": now.isoformat(),
    })


# ----------------------------------------------------------------------
# stop
# ----------------------------------------------------------------------


@wrap_tool
async def issue_agent_stop(app: GhiaApp) -> ToolResponse:
    """Pause the agent without destroying history.

    We keep ``completed`` and ``skipped`` intact so the response can
    report "X issues completed this session" to the user.  Runtime
    fields that only make sense while active (``active_issue``,
    ``poll_timer_active``, ``queue``) are cleared.
    """

    async with app.session.lock:
        current = await app.session.read()
        completed_count = len(current.completed)
        skipped_count = len(current.skipped)

        new_state = SessionState.model_validate({
            **current.model_dump(),
            "status": "idle",
            "active_issue": None,
            "poll_timer_active": False,
            # Leave ``mode``, ``completed``, ``skipped``,
            # ``discovered_conventions`` alone — they're informative
            # across pause/resume and the UI shows them.
        })
        app.session._persist(new_state)

    message = (
        f"Agent paused. {completed_count} issues completed this session."
    )
    return ok({
        "message": message,
        "completed_count": completed_count,
        "skipped_count": skipped_count,
    })


# ----------------------------------------------------------------------
# status
# ----------------------------------------------------------------------


@wrap_tool
async def issue_agent_status(app: GhiaApp) -> ToolResponse:
    """Return a snapshot of the current SessionState + human summary.

    No lock is acquired — ``SessionStore.read`` is already a safe
    snapshot.  This tool should be side-effect-free and must remain
    cheap so UIs can poll it.
    """

    state = await app.session.read()
    payload = _snapshot_dict(state)
    payload["summary"] = (
        f"Agent is {state.status} "
        f"(mode={state.mode}, "
        f"queue={len(state.queue)}, "
        f"completed={len(state.completed)}, "
        f"skipped={len(state.skipped)})"
    )
    return ok(payload)


# ----------------------------------------------------------------------
# set_mode
# ----------------------------------------------------------------------


@wrap_tool
async def issue_agent_set_mode(app: GhiaApp, mode: str) -> ToolResponse:
    """Validate ``mode`` and persist it.

    AC-007-3/4/5 require the new mode to take effect immediately — we
    satisfy that by persisting to ``session.json`` inside the lock, so
    the very next ``issue_agent_status`` call (and every subsequent
    control-flow decision in the agent) will see the new value.
    """

    if mode not in _VALID_MODES:
        return err(
            ErrorCode.INVALID_INPUT,
            f"mode must be one of {sorted(_VALID_MODES)!r} (got {mode!r})",
        )

    async with app.session.lock:
        current = await app.session.read()
        new_state = SessionState.model_validate({
            **current.model_dump(),
            "mode": mode,
        })
        app.session._persist(new_state)

    return ok({
        "mode": mode,
        "message": f"Mode switched to {mode}. Takes effect immediately.",
    })


# ----------------------------------------------------------------------
# fetch_now (STUB)
# ----------------------------------------------------------------------


@wrap_tool
async def issue_agent_fetch_now(app: GhiaApp) -> ToolResponse:
    """STUB: real implementation lands with TRD-016/030 (Cluster 4/6).

    We return ok(...) rather than an error so the MCP surface is
    stable from Sprint 1 onward — callers integrating against the
    tool set can rely on the shape of the response without needing to
    branch on "is fetch_now wired up yet".
    """

    logger.info("fetch_now called; stub — issue fetching lands in Cluster 4")
    return ok({
        "message": "fetch_now stub — issue fetching lands in Cluster 4",
        "fetched": 0,
    })
