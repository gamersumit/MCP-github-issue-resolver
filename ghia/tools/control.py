"""Control-plane tools (TRD-012).

Five user-visible MCP tools that govern the agent's lifecycle:

* ``issue_agent_start`` — transition to active, discover conventions,
  render protocol [REQ-005]
* ``issue_agent_stop``  — pause the agent [REQ-007]
* ``issue_agent_status``— read-only snapshot of the session [REQ-008]
* ``issue_agent_set_mode`` — swap semi/full mid-session [REQ-009]
* ``issue_agent_fetch_now`` — trigger one polling tick out-of-band [REQ-005]

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

from ghia import polling
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


# Fallback shown in the protocol banner when we can't detect the repo's
# default branch (e.g. the very first start, before any tool has hit
# the GitHub API). Per-issue branch creation always re-resolves the
# default at runtime via gh, so this fallback is purely cosmetic.
_DEFAULT_BRANCH_FALLBACK = "main"

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
         ``mode=app.config.mode`` (the wizard's choice, NOT the stale
         session value), ``discovered_conventions=<summary>``,
         ``default_branch=<fallback>``, ``repo=<auto-detected>``.
      4. Trigger one immediate polling tick so the queue is populated
         on first start (instead of making the user wait for the first
         scheduled poll, which can be up to ``poll_interval_min``
         minutes away).
      5. Start the background polling task.
      6. Render the protocol string with the freshly-populated queue
         and a human-friendly summary of the label filter.
      7. Return ``{protocol, mode, queue, label_summary,
         discovered_conventions_preview}``.
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
        # The wizard's ``mode`` choice (``app.config.mode``) is the
        # source of truth on every fresh start. ``set_mode`` is the
        # only mid-session override; preserving session.mode here was
        # a v0.2.0 bug that made "full" configs silently start in
        # "semi" mode.
        new_state = SessionState.model_validate({
            **current.model_dump(),
            "status": "active",
            "mode": app.config.mode,
            "repo": app.repo_full_name,
            "session_started": now,
            "discovered_conventions": discovered,
            "default_branch": _DEFAULT_BRANCH_FALLBACK,
        })
        app.session._persist(new_state)

    # First-start fetch — runs OUTSIDE the lock so the tick can
    # acquire the lock for its own queue mutations. Errors are
    # swallowed (logged) so a transient gh failure doesn't block
    # ``start`` itself; the background poller will retry.
    try:
        await polling._tick_once(app)
    except Exception as exc:  # noqa: BLE001 — never block start on fetch
        logger.warning("initial fetch on start failed: %s", exc)

    # Re-read so the rendered protocol shows the queue we just
    # populated rather than the empty pre-fetch snapshot.
    new_state = await app.session.read()

    queue_summary = format_queue_summary(new_state.queue)
    protocol = render_protocol(
        repo=new_state.repo or app.repo_full_name,
        mode=new_state.mode,
        default_branch=new_state.default_branch or _DEFAULT_BRANCH_FALLBACK,
        discovered_conventions=discovered,
        queue_summary=queue_summary,
        timestamp=_human_timestamp(now),
    )

    # Start the background poller AFTER all state is persisted and the
    # initial fetch has run. The poller is the lone "ambient" piece of
    # the agent — every other tool is request/response — so its
    # lifecycle has to be bound tightly to start/stop or it will leak
    # across sessions.
    await polling.start_polling(app)

    preview = (discovered or "")[:200]
    return ok({
        "protocol": protocol,
        "mode": new_state.mode,
        "queue": list(new_state.queue),
        "label_filter": _label_filter_summary(app.config.labels),
        "discovered_conventions_preview": preview,
        "session_started": now.isoformat(),
    })


def _label_filter_summary(labels: list[str]) -> str:
    """One-line human summary of the configured label filter.

    Used in the start tool's response so the user immediately sees
    which issues will be picked up — the #1 source of "why didn't it
    fetch my issue?" confusion.
    """

    if not labels:
        return "no filter — every open issue will be picked up"
    if len(labels) == 1:
        return f"only issues labelled '{labels[0]}'"
    quoted = ", ".join(f"'{label}'" for label in labels)
    return f"issues labelled any of: {quoted}"


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

    # Cancel the poller BEFORE flipping status — otherwise a tick
    # firing right as we transition could re-enter list_issues against
    # a half-stopped session.  stop_polling is a no-op when no task is
    # active, so calling it here is always safe.
    await polling.stop_polling(app)

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
    """Validate ``mode`` and persist it to both session and config.

    AC-007-3/4/5 require the new mode to take effect immediately — we
    persist to ``session.json`` so the very next status call (and
    every subsequent control-flow decision) sees the new value.

    We ALSO persist to the per-repo config file so the choice survives
    ``stop`` → ``start``: the start tool reads ``app.config.mode``
    as its source of truth, so without this dual-write a set_mode
    call would silently revert on the next start.
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

    # Mirror to the on-disk config so the next start picks the same
    # mode. ``app.config`` is the live model — we mutate it AFTER the
    # save_config call so a write failure leaves the in-memory copy
    # unchanged. ``app.config_path`` is None for some tests that build
    # the dataclass directly; in that case skip persistence and just
    # mutate the in-memory copy so the test's view of mode stays
    # consistent.
    new_cfg = app.config.model_copy(update={"mode": mode})
    if app.config_path is not None:
        try:
            from ghia.config import save_config

            save_config(new_cfg, path=app.config_path)
        except Exception as exc:  # noqa: BLE001 — best-effort persistence
            logger.warning(
                "set_mode persisted session but failed to update config: %s", exc
            )
    app.config = new_cfg

    return ok({
        "mode": mode,
        "message": f"Mode switched to {mode}. Takes effect immediately.",
    })


# ----------------------------------------------------------------------
# fetch_now (STUB)
# ----------------------------------------------------------------------


@wrap_tool
async def issue_agent_fetch_now(app: GhiaApp) -> ToolResponse:
    """Trigger one polling tick out-of-band.

    Runs a single iteration of the polling work (fetch issues, update
    ``last_fetched``) without disturbing the running poller.  Errors
    inside the tick are caught here and surfaced as a structured
    response — the goal is for the user to see WHY a manual refresh
    failed, rather than the silent "WARNING in logs only" treatment
    that the background loop applies.
    """

    try:
        await polling._tick_once(app)
    except Exception as exc:  # noqa: BLE001 — surface to the user, don't crash
        logger.warning("fetch_now tick failed: %s", exc)
        return err(
            ErrorCode.NETWORK_ERROR,
            f"fetch_now failed: {type(exc).__name__}: {exc}",
        )

    state = await app.session.read()
    return ok({
        "message": "fetch triggered",
        "last_fetched": state.last_fetched.isoformat()
        if state.last_fetched is not None
        else None,
    })
