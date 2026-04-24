"""UI opener — picks browser vs terminal and orchestrates the flow (TRD-021).

Responsibilities:

* :func:`is_headless` — single source of truth for the "can I open a
  browser?" question.  Honours explicit overrides
  (``GHIA_FORCE_TERMINAL`` / ``GHIA_FORCE_BROWSER``) so tests and
  power-users on weird setups can pin the path.
* :func:`open_picker` — orchestrator.  In a display environment it
  spins up the Starlette ASGI app on ``127.0.0.1:port``, opens the
  user's default browser at that URL, and awaits an
  :class:`asyncio.Event` set by the ``/api/confirm`` route.  On
  timeout (or in a headless env from the start) it drops to the
  :func:`ghia.ui.terminal.pick_issues_terminal` flow and returns its
  result.

Why an Event and not session-polling: polling would require the
browser route to write a sentinel timestamp on the session, then the
opener to re-read the file every N ms.  An :class:`asyncio.Event`
delivers the signal in O(1) latency and keeps the session schema free
of UI-only fields.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import webbrowser
from typing import Any, Optional

from ghia.app import GhiaApp

logger = logging.getLogger(__name__)

__all__ = ["is_headless", "open_picker"]


# Wait this long for the user to click Confirm before falling back.
# 10 minutes mirrors the TRD's "leave the picker open while you go
# grab coffee" assumption; tests pin a short value via the kwarg.
_DEFAULT_PICKER_TIMEOUT_SEC: float = 600.0


def is_headless() -> bool:
    """Return True iff a browser cannot reasonably be opened.

    Decision tree (first match wins):

    1. ``GHIA_FORCE_BROWSER=1`` → False unconditionally.  Power-users
       with a custom xdg-open / browser launcher who *know* a browser
       is available can bypass our heuristics.
    2. ``GHIA_FORCE_TERMINAL=1`` → True unconditionally.  Same idea
       in the other direction; also what tests use to pin the path.
    3. ``SSH_CONNECTION`` is set → True.  Even with X11 forwarding the
       picker is awkward over SSH; better to default to the terminal
       path and let the user override with ``GHIA_FORCE_BROWSER=1``
       if they really do have a usable display.
    4. On Linux: True when BOTH ``$DISPLAY`` and ``$WAYLAND_DISPLAY``
       are empty AND ``xdg-open`` is missing.  All three signals
       together are a strong "no display" hint; any one alone might
       be flaky (a wayland-only session may have ``$DISPLAY`` empty
       but a working browser launcher).
    5. Otherwise → False.  macOS/Windows have native ``open`` /
       ``start`` and ``webbrowser.open`` handles them reliably.
    """

    # Explicit overrides take priority over every other signal.  Order
    # matters: GHIA_FORCE_BROWSER beats GHIA_FORCE_TERMINAL because
    # "I know I have a browser" is a stronger statement than "default
    # to terminal" — a user who sets both probably copy-pasted from
    # docs and we should pick the more permissive interpretation.
    if os.environ.get("GHIA_FORCE_BROWSER", "") == "1":
        return False
    if os.environ.get("GHIA_FORCE_TERMINAL", "") == "1":
        return True

    if os.environ.get("SSH_CONNECTION", ""):
        return True

    if sys.platform == "linux":
        no_x = not os.environ.get("DISPLAY", "")
        no_wayland = not os.environ.get("WAYLAND_DISPLAY", "")
        no_xdg = shutil.which("xdg-open") is None
        if no_x and no_wayland and no_xdg:
            return True

    return False


async def _await_event_with_timeout(
    event: asyncio.Event, timeout: float
) -> bool:
    """Wait for ``event`` up to ``timeout`` seconds.

    Returns True if the event fired in time, False on timeout.  We
    factor this out so :func:`open_picker` reads linearly (the
    ``asyncio.wait_for`` + ``TimeoutError`` dance is fiddly inline).
    """

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return False
    return True


async def open_picker(
    app: GhiaApp,
    *,
    port: int = 4242,
    timeout_sec: float = _DEFAULT_PICKER_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Show the picker (browser or terminal) and return the user's choice.

    Returns a dict matching the ``/api/confirm`` payload shape:
    ``{"queue": [int...], "mode": "semi"|"full"}``.

    On headless systems (or when forced via env var) the browser path
    is skipped entirely — :func:`ghia.ui.terminal.pick_issues_terminal`
    handles the prompting and persistence flow.

    On a display system: starts a uvicorn server on ``127.0.0.1:port``,
    opens the user's default browser at that URL, and awaits the
    confirm event the ``/api/confirm`` route will set.  On timeout we
    log + tear down the server and fall back to the terminal picker so
    the user is never left without a way to make the selection.

    Args:
        app: Composition root.
        port: TCP port for the local picker server.  Default 4242 per
            TRD; tests override.
        timeout_sec: How long to wait for the user to click Confirm
            before falling back to the terminal picker.  Default 10
            minutes.

    Returns:
        The chosen ``{queue, mode}`` dict.  Always returns; on every
        error path we fall back to the terminal picker rather than
        raising.
    """

    # Lazy imports keep the headless path free of any UI HTTP code,
    # so a server with no httpx/uvicorn pre-imported doesn't pay the
    # cost just to render a `rich` table.
    from ghia.ui.terminal import pick_issues_terminal

    if is_headless():
        app.logger.info("UI opener: headless detected, using terminal picker")
        return await pick_issues_terminal(app)

    from ghia.ui.server import run_ui_server

    confirm_event = asyncio.Event()
    server = run_ui_server(
        app, host="127.0.0.1", port=port, confirm_event=confirm_event
    )

    serve_task = asyncio.create_task(server.serve())
    try:
        # Give uvicorn a tick to bind the socket before we send the
        # browser at it; otherwise we race the listener.  We don't
        # block on a "server ready" event because uvicorn's startup is
        # fast enough that 50ms is comfortably more than we need.
        await asyncio.sleep(0.05)

        url = f"http://127.0.0.1:{port}/"
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001 — webbrowser.open should never crash us
            app.logger.warning("webbrowser.open failed: %s", exc)

        ok = await _await_event_with_timeout(confirm_event, timeout_sec)
        if not ok:
            app.logger.warning(
                "picker timed out after %s sec; falling back to terminal",
                timeout_sec,
            )
            # The browser may still be open at this point, but we need
            # to give the user *some* path to make a selection — the
            # terminal picker is the safe fallback.
            terminal_result = await pick_issues_terminal(app)
            return terminal_result

        # Read back what the confirm route persisted so the caller
        # sees exactly what's on disk (single source of truth).
        state = await app.session.read()
        return {"queue": list(state.queue), "mode": state.mode}
    finally:
        # Always clean up the listener — leaking a uvicorn loop after
        # the picker returns would keep the port held.
        server.should_exit = True
        try:
            # Give uvicorn a brief moment to shut down gracefully;
            # then cancel as a backstop.
            await asyncio.wait_for(serve_task, timeout=2.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            serve_task.cancel()
            try:
                await serve_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
