"""Serial queue processor (TRD-029).

Models the "process one issue at a time, pause cleanly on transient
network failures" contract that the agent protocol orchestration layer
relies on.  This module is intentionally state-less — the queue,
``active_issue``, ``completed`` and ``skipped`` lists all live in
:class:`ghia.session.SessionStore`, and every mutation goes through
its lock so a concurrent ``skip_issue`` (or any other writer) sees a
consistent picture.

The actual per-issue work (open the issue, plan a fix, run tests, open
a PR) is driven by the agent protocol via the MCP tool layer, not by
this processor.  The handler argument exists so tests can plug in a
deterministic stub and so the real orchestration code can drive the
loop with a closure.

Key invariants:

* **Serial** — exactly one ``active_issue`` at a time.  We await the
  handler to a terminal :class:`ToolResponse` before advancing.
* **Pause-don't-crash on transport failure** — a handler returning
  ``NETWORK_ERROR`` or ``RATE_LIMITED`` leaves the issue at the head
  of the queue, clears ``active_issue`` so resume re-picks it, and
  returns with ``paused=True``.  No state is lost.
* **Skip on logical failure** — anything else moves the issue to
  ``skipped`` and the loop continues.  This is what differentiates a
  "the user's GitHub creds expired, pause and tell them" failure from
  a "this one issue is malformed, move on" failure.

Satisfies REQ-014.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from ghia.app import GhiaApp
from ghia.errors import ErrorCode, ToolResponse, ok

__all__ = [
    "QueueHandler",
    "process_queue",
]


# A handler turns ``(app, issue_number)`` into a terminal ToolResponse.
# We type it as ``Callable`` rather than ``Protocol`` to keep the
# import surface small — callers pass plain ``async def`` functions.
QueueHandler = Callable[[GhiaApp, int], Awaitable[ToolResponse]]


# Codes that mean "transient transport failure — pause, don't skip".
# Centralized so the rule is auditable and a future addition (e.g. a
# hypothetical NEW transient code) only needs to be added once.
_PAUSE_CODES = frozenset({ErrorCode.NETWORK_ERROR, ErrorCode.RATE_LIMITED})


# Threshold for the "queue is getting long" warning.  TRD-029 calls
# this out as a UX hint — the user can drain or trim the queue if it
# grows unbounded.  Constant rather than configurable because the
# threshold is a sanity check, not a tuning knob.
_QUEUE_WARN_THRESHOLD = 10


async def _default_handler(_app: GhiaApp, _number: int) -> ToolResponse:
    """Stub handler used when the caller doesn't supply one.

    The real handler is the agent protocol orchestration: this stub
    exists so the processor can be exercised end-to-end (and tested)
    without dragging in the full protocol stack.  Marking each issue
    "completed" lets tests assert on the queue-drain semantics.
    """

    return ok({"completed": True})


async def process_queue(
    app: GhiaApp,
    *,
    handler: Optional[QueueHandler] = None,
) -> dict[str, Any]:
    """Drain the session queue, one issue at a time.

    Args:
        app: The composition root.  Reads ``app.session.queue`` for the
            initial work list and writes back ``active_issue``,
            ``completed`` and ``skipped`` as the loop advances.
        handler: Async callable invoked per issue.  Defaults to
            :func:`_default_handler` (always-completed stub) so callers
            that only want to verify the contract can do so without
            wiring a full protocol driver.

    Returns:
        ``{"processed": [...], "skipped": [...], "remaining": [...],
        "paused": bool}`` reflecting the run.  ``paused`` is true iff
        the loop exited early on a transient transport failure — the
        last issue in ``remaining`` is the one that failed and is still
        at the head of the persisted queue, ready to be re-picked on
        resume.
    """

    if handler is None:
        handler = _default_handler

    initial_state = await app.session.read()
    initial_queue = list(initial_state.queue)

    if len(initial_queue) > _QUEUE_WARN_THRESHOLD:
        # Hint, not error — the user can keep going.  We log via the
        # app's named logger so the message lands wherever the rest of
        # the agent's output goes (and inherits redaction).
        app.logger.warning(
            "queue has %d items; consider trimming",
            len(initial_queue),
        )

    processed: list[int] = []
    skipped_run: list[int] = []
    paused = False
    pause_reason: Optional[str] = None

    # Iterate over the snapshot, not the live state — we want a
    # deterministic plan for THIS run, even if a concurrent
    # pick_issue races with us.  New picks land in the persisted
    # queue and will be drained on the next process_queue invocation.
    for number in initial_queue:
        # Mark the issue active before invoking the handler so an
        # external observer (status tool, UI poll) can see what we're
        # working on.
        await app.session.update(active_issue=number)

        response = await handler(app, number)

        if response.success and isinstance(response.data, dict) and response.data.get(
            "completed"
        ):
            # Happy path: move the issue from queue → completed under
            # the lock so the on-disk state always reflects exactly
            # one transition at a time.
            async with app.session.lock:
                current = await app.session.read()
                new_queue = [n for n in current.queue if n != number]
                new_completed = list(current.completed)
                if number not in new_completed:
                    new_completed.append(number)
                merged = current.model_copy(
                    update={
                        "queue": new_queue,
                        "completed": new_completed,
                        "active_issue": None,
                    }
                )
                app.session._persist(merged)
            processed.append(number)
            continue

        if not response.success and response.code in _PAUSE_CODES:
            # Transient transport failure: clear active_issue so a
            # resume re-picks the same issue, but DO NOT touch the
            # queue — issue stays at the head where it was.
            await app.session.update(active_issue=None)
            paused = True
            pause_reason = response.code.value if response.code else "UNKNOWN"
            app.logger.warning(
                "queue paused on issue %d: %s",
                number,
                response.error or pause_reason,
            )
            break

        # Any other failure: skip this issue and continue.  We log at
        # WARNING with the structured code so operators can grep for
        # patterns ("which error codes show up here most?").
        async with app.session.lock:
            current = await app.session.read()
            new_queue = [n for n in current.queue if n != number]
            new_skipped = list(current.skipped)
            if number not in new_skipped:
                new_skipped.append(number)
            merged = current.model_copy(
                update={
                    "queue": new_queue,
                    "skipped": new_skipped,
                    "active_issue": None,
                }
            )
            app.session._persist(merged)
        skipped_run.append(number)
        app.logger.warning(
            "queue skipped issue %d: code=%s error=%s",
            number,
            response.code.value if response.code else "?",
            response.error or "",
        )

    # ``remaining`` is read fresh from persisted state — that way we
    # report the truth (including any concurrent pick_issue additions)
    # rather than what we *thought* would be left at the start.
    final_state = await app.session.read()
    result: dict[str, Any] = {
        "processed": processed,
        "skipped": skipped_run,
        "remaining": list(final_state.queue),
        "paused": paused,
    }
    if pause_reason is not None:
        result["reason"] = pause_reason
    return result
