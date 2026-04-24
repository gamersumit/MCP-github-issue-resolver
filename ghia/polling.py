"""Background polling task (TRD-030).

Wraps the "fetch new issues every N minutes" behavior in an
``asyncio.Task`` lifecycle that the control-plane tools can start and
stop.  The task is owned by :class:`~ghia.app.GhiaApp`: exactly one
poller per app instance, stored on ``app._polling_task``.

Three guarantees:

1. **Cancellation-aware** — :func:`stop_polling` cancels the task and
   awaits its completion, so a clean ``issue_agent_stop`` never leaves
   a zombie poller running.
2. **Failure-tolerant** — a failing tick is logged at WARNING and the
   loop continues.  Transient errors (a flaky network, a 5xx from
   GitHub) must not crash the polling loop — that would defeat the
   point of having one.
3. **No hot-loop on errors** — every tick (success OR failure) is
   followed by ``poll_interval_min * 60`` seconds of sleep, so a
   sustained outage doesn't burn CPU or hammer the API.

Satisfies REQ-016.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from ghia.app import GhiaApp

logger = logging.getLogger(__name__)

__all__ = [
    "PollTickHandler",
    "polling_loop",
    "start_polling",
    "stop_polling",
]


PollTickHandler = Callable[[GhiaApp], Awaitable[None]]


async def _tick_once(app: GhiaApp) -> None:
    """Run one fetch-and-record iteration.

    Imports :mod:`ghia.tools.issues` lazily to avoid a circular import
    (issues → app, polling → app, control → polling): keeping the
    module-level import set narrow makes the module graph easier to
    reason about.

    The ``last_fetched`` timestamp is the source of truth for "when did
    we last sync with GitHub?" — UIs and the status tool surface it to
    the user.  We update it AFTER the fetch returns so a failed fetch
    doesn't lie about when we last had fresh data.
    """

    from ghia.tools import issues as issues_tools

    await issues_tools.list_issues(app)
    await app.session.update(last_fetched=datetime.now(tz=timezone.utc))


async def polling_loop(
    app: GhiaApp,
    *,
    on_tick: Optional[PollTickHandler] = None,
) -> None:
    """Run the polling loop until cancelled.

    Args:
        app: Composition root; provides config (poll interval) and
            logger.
        on_tick: Optional override for the per-iteration work.  Tests
            inject a counter / cancelling stub here; production uses
            the default :func:`_tick_once`.

    The loop sleeps ``app.config.poll_interval_min * 60`` seconds
    between iterations.  Cancellation is honored at the next ``await``
    boundary (the ``sleep`` is the most common one) and propagates
    cleanly via :class:`asyncio.CancelledError`.
    """

    handler = on_tick if on_tick is not None else _tick_once
    interval_seconds = app.config.poll_interval_min * 60

    try:
        while True:
            try:
                await handler(app)
            except asyncio.CancelledError:
                # Re-raise so the outer ``try/except`` can run its
                # cleanup; otherwise we'd swallow our own cancel.
                raise
            except Exception as exc:  # noqa: BLE001 — never crash the loop
                # WARNING (not ERROR) because a single failed tick is
                # an expected condition — flaky network, transient
                # 5xx, etc.  Operators who need stricter alerting can
                # filter on the message.
                app.logger.warning("poll tick failed: %s", exc)

            await asyncio.sleep(interval_seconds)
    except asyncio.CancelledError:
        # Mark the timer inactive on the way out so a subsequent
        # ``status`` call accurately reflects "no poller running".
        await app.session.update(poll_timer_active=False)
        raise


async def start_polling(app: GhiaApp) -> asyncio.Task:
    """Spawn the polling task and stash it on the app.

    Idempotent in spirit, not in fact: calling this twice replaces the
    handle but leaks the prior task.  Callers should pair every
    ``start_polling`` with a ``stop_polling`` — the control-plane
    ``start``/``stop`` tools enforce that pairing for the user.
    """

    task = asyncio.create_task(polling_loop(app), name="ghia-poller")
    app._polling_task = task  # type: ignore[attr-defined]
    await app.session.update(poll_timer_active=True)
    return task


async def stop_polling(app: GhiaApp) -> None:
    """Cancel the running poller and clear the handle.

    Safe to call when no poller is running — it's a no-op in that
    case, which keeps ``issue_agent_stop`` simple (no need to branch
    on "did we start one?").

    The ``return_exceptions=True`` on :func:`asyncio.gather` swallows
    the :class:`asyncio.CancelledError` that the task re-raises during
    shutdown — that's the expected exit path, not a real failure.
    """

    task: Optional[asyncio.Task] = getattr(app, "_polling_task", None)
    if task is not None and not task.done():
        task.cancel()
        # gather (rather than await directly) so a CancelledError
        # propagated by the task doesn't escape this function.
        await asyncio.gather(task, return_exceptions=True)

    app._polling_task = None  # type: ignore[attr-defined]
    # Always write the flag, even if no task was running, so a manual
    # ``stop_polling`` after a crash that leaked the flag still tidies
    # up.
    await app.session.update(poll_timer_active=False)
