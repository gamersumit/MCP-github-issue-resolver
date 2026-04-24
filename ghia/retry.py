"""Retry wrapper for full-auto execution (TRD-027).

A decorator factory — :func:`with_retries` — that wraps an async
tool handler and re-runs it up to N times when the result indicates
failure.  Two failure flavours are recognized:

1. ``ToolResponse.success is False`` — the tool itself errored.
2. ``ToolResponse.success is True and data["passed"] is False`` —
   the tool ran cleanly but its semantic check (lint or test)
   failed.  This is the primary case for the lint+test loop.

On the final failed attempt we apply the ``human-review`` label to
the active issue (if any) so a human can pick it up.  Labelling is
best-effort — a failure to label does not propagate; we still
return the last :class:`ToolResponse` so callers can act on it.

Satisfies REQ-022.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Awaitable, Callable

from ghia.app import GhiaApp
from ghia.errors import ToolResponse
from ghia.integrations import gh_cli
from ghia.integrations.gh_cli import GhAuthError

logger = logging.getLogger(__name__)

__all__ = ["with_retries"]


def _is_failed(resp: ToolResponse) -> bool:
    """True when the response should trigger another attempt.

    Treats both transport errors and semantic-failed-success as
    failures so callers don't have to special-case the lint+test
    pattern at every call site.
    """

    if not resp.success:
        return True
    data = resp.data
    if isinstance(data, dict) and data.get("passed") is False:
        return True
    return False


async def _label_human_review(
    app: GhiaApp, label: str
) -> None:
    """Best-effort: label the active issue for human attention.

    Swallows :class:`GhAuthError` because a labelling failure on
    the failure path would mask the real test/lint failure the
    caller actually needs to see.  Logs the failure at WARNING so
    operators can still investigate.
    """

    state = await app.session.read()
    issue_number = state.active_issue
    if issue_number is None:
        # Nothing to label — caller probably ran a retry path
        # outside of an active issue context (e.g. a manual lint
        # invocation).  Silent skip.
        return

    try:
        await gh_cli.add_label(app.repo_full_name, issue_number, label)
    except GhAuthError as exc:
        # Don't escalate — the original failure is what the caller
        # cares about.  We log so the failure isn't invisible.
        logger.warning(
            "could not apply %r label to issue #%d: %s",
            label,
            issue_number,
            exc.message,
        )


def with_retries(
    *,
    max_attempts: int = 3,
    label_on_failure: str = "human-review",
) -> Callable[
    [Callable[..., Awaitable[ToolResponse]]],
    Callable[..., Awaitable[ToolResponse]],
]:
    """Decorator factory: re-run an async tool until it succeeds.

    The wrapped function must take :class:`GhiaApp` as its first
    positional argument (or ``app=...`` keyword) so the wrapper can
    drive labelling without a separate plumbing channel.

    Args:
        max_attempts: Total number of attempts including the first.
            Default 3 per AC-022-1.
        label_on_failure: Label name applied to the active issue
            when all attempts fail.

    Returns:
        Decorator that produces a wrapped async function returning
        the last :class:`ToolResponse` seen.
    """

    if max_attempts < 1:
        raise ValueError(
            f"max_attempts must be >= 1, got {max_attempts}"
        )

    def _decorator(
        func: Callable[..., Awaitable[ToolResponse]],
    ) -> Callable[..., Awaitable[ToolResponse]]:
        @functools.wraps(func)
        async def _wrapper(*args: Any, **kwargs: Any) -> ToolResponse:
            # Pull the GhiaApp out of the call args so we can label
            # without forcing the caller to thread it through twice.
            app: GhiaApp = (
                args[0] if args and isinstance(args[0], GhiaApp)
                else kwargs.get("app")
            )

            last: ToolResponse | None = None
            for attempt in range(1, max_attempts + 1):
                last = await func(*args, **kwargs)
                if not _is_failed(last):
                    if app is not None:
                        app.logger.info(
                            "retry %s succeeded on attempt %d/%d",
                            func.__qualname__,
                            attempt,
                            max_attempts,
                        )
                    return last
                if app is not None:
                    app.logger.info(
                        "retry %s failed attempt %d/%d (success=%s)",
                        func.__qualname__,
                        attempt,
                        max_attempts,
                        last.success,
                    )

            # All attempts exhausted — try to label the active issue
            # so a human can intervene.
            if app is not None:
                await _label_human_review(app, label_on_failure)

            # ``last`` is non-None: the loop ran at least once
            # because max_attempts >= 1 is enforced above.
            assert last is not None
            return last

        return _wrapper

    return _decorator
