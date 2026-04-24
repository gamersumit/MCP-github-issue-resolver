"""Starlette sub-app that hosts the picker UI (TRD-018).

Three routes:

* ``GET /``             — serves ``ui_static/picker.html`` verbatim.
* ``GET /api/issues``   — proxies :func:`ghia.tools.issues.list_issues`
                          and returns the ``ToolResponse`` envelope as
                          JSON.
* ``POST /api/confirm`` — accepts ``{queue: [int...], mode: "semi"|"full"}``,
                          validates with Pydantic, and writes both
                          fields to ``SessionStore`` under its lock.

Design notes:

* The app is intentionally constructed *per call* (no module-level
  singleton) so each :func:`build_ui_app` invocation is a clean
  closure over a specific :class:`GhiaApp`.  This keeps tests
  trivially independent and matches the "tools take an app" pattern
  used everywhere else in the codebase.
* :func:`build_ui_app` accepts an optional ``confirm_event``
  (``asyncio.Event``).  ``/api/confirm`` sets it after a successful
  write, which lets :mod:`ghia.ui.opener` await user confirmation
  without polling the session file.  The parameter is optional so the
  Starlette app remains usable in tests that don't care about the
  signal.
* The bind address is hard-wired to ``127.0.0.1`` in
  :func:`run_ui_server` — never ``0.0.0.0`` — because exposing the
  picker (which can write to the agent's session) on a non-loopback
  interface is a security hazard.  This is a hard rule, not a default.
* The picker HTML is resolved relative to ``<repo_root>/ui_static``
  where ``<repo_root>`` is the package install location (``ghia/``'s
  parent's parent, since this file lives at ``ghia/ui/server.py``).
  If the file is missing — for example because the package was
  installed without the static asset — ``GET /`` returns a 503 with a
  structured error body so the failure is visible rather than silent.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

import uvicorn
from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from ghia.app import GhiaApp
from ghia.errors import ErrorCode
from ghia.tools.issues import list_issues

logger = logging.getLogger(__name__)

__all__ = [
    "build_ui_app",
    "run_ui_server",
    "picker_html_path",
    "ConfirmPayload",
    "UI_BIND_HOST",
]


# Loopback only.  The picker UI mutates session state — exposing it on
# a non-loopback interface would let any host on the network select an
# arbitrary issue queue and put the agent into ``active``.  We treat
# this constant as a hard rule and reuse it in tests.
UI_BIND_HOST: str = "127.0.0.1"


def picker_html_path() -> Path:
    """Resolve ``ui_static/picker.html`` relative to the package root.

    ``__file__`` is ``<repo_root>/ghia/ui/server.py``; the static asset
    lives at ``<repo_root>/ui_static/picker.html``.  We compute the
    path lazily (each call) so a test that patches ``__file__`` or
    runs from an editable install picks up the right location.
    """

    return Path(__file__).resolve().parent.parent.parent / "ui_static" / "picker.html"


class ConfirmPayload(BaseModel):
    """JSON body the picker POSTs to ``/api/confirm``.

    ``mode`` must be one of ``"semi" | "full"``; we reject the request
    rather than silently coerce so a buggy client surfaces immediately
    instead of quietly defaulting.  ``queue`` items must be positive
    ints (issue numbers) and are de-duplicated while preserving order
    so re-clicking a card never inflates the queue.
    """

    queue: list[int] = Field(default_factory=list)
    mode: str

    @field_validator("mode")
    @classmethod
    def _mode_must_be_known(cls, v: str) -> str:
        if v not in ("semi", "full"):
            raise ValueError(
                "mode must be 'semi' or 'full' (got " + repr(v) + ")"
            )
        return v

    @field_validator("queue")
    @classmethod
    def _queue_items_must_be_positive(cls, v: list[int]) -> list[int]:
        # Pydantic already coerces to int; we additionally insist on
        # positivity since GitHub issue numbers start at 1.
        for n in v:
            if not isinstance(n, int) or n <= 0:
                raise ValueError(
                    "queue items must be positive integers (issue numbers)"
                )
        # Stable de-dupe — preserve first occurrence.
        seen: set[int] = set()
        out: list[int] = []
        for n in v:
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out


# --------------------------------------------------------------------------
# Route handlers
# --------------------------------------------------------------------------


def _envelope_failure(code: ErrorCode, message: str, *, status: int) -> JSONResponse:
    """Return a ``ToolResponse``-shaped error JSON with the given status.

    The picker JS only knows the ``{success, data, error, code}``
    envelope — emitting anything else (e.g. Starlette's own 422 body)
    would force the client to special-case error shapes per route.
    """

    return JSONResponse(
        {
            "success": False,
            "data": None,
            "error": message,
            "code": code.value,
        },
        status_code=status,
    )


def _make_index_handler(app: GhiaApp) -> Any:
    """``GET /`` → serve ``picker.html`` from disk."""

    async def index(_request: Request) -> Response:
        path = picker_html_path()
        if not path.is_file():
            # Surfacing this as an envelope-shaped 503 makes the
            # missing-asset failure observable in the same shape as
            # every other error the agent emits.
            app.logger.warning("picker.html missing at %s", path)
            return _envelope_failure(
                ErrorCode.FILE_NOT_FOUND,
                f"picker.html not found at {path}",
                status=503,
            )
        return FileResponse(path, media_type="text/html")

    return index


def _make_issues_handler(app: GhiaApp) -> Any:
    """``GET /api/issues`` → proxy :func:`list_issues` as JSON."""

    async def get_issues(_request: Request) -> Response:
        # ``list_issues`` is decorated with ``wrap_tool`` so it never
        # raises; we still expose its envelope verbatim.  HTTP status
        # follows the envelope — 200 on success, 500 on tool failure
        # (the body still carries the structured error so the client
        # can show a useful message rather than "HTTP 500").
        resp = await list_issues(app)
        payload = resp.model_dump(mode="json")
        status = 200 if resp.success else 500
        return JSONResponse(payload, status_code=status)

    return get_issues


def _make_confirm_handler(
    app: GhiaApp,
    confirm_event: Optional[asyncio.Event],
) -> Any:
    """``POST /api/confirm`` → write ``queue`` + ``mode`` to the session.

    Closure captures ``confirm_event`` so :mod:`ghia.ui.opener` can
    await user confirmation without polling the session file.  When
    the event is ``None`` (e.g. the test harness or callers that
    drive the server directly) we simply skip the signal.
    """

    async def confirm(request: Request) -> Response:
        try:
            raw = await request.json()
        except Exception as exc:  # noqa: BLE001 — body parsing is best-effort
            return _envelope_failure(
                ErrorCode.INVALID_INPUT,
                f"request body must be valid JSON: {exc}",
                status=400,
            )

        try:
            payload = ConfirmPayload.model_validate(raw)
        except ValidationError as exc:
            # Pydantic's own error JSON is verbose; we collapse it to a
            # single human-readable string while still preserving the
            # field names so the client can highlight the right input.
            messages = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                for e in exc.errors()
            )
            return _envelope_failure(
                ErrorCode.INVALID_INPUT,
                messages or "invalid payload",
                status=400,
            )

        try:
            new_state = await app.session.update(
                queue=payload.queue, mode=payload.mode
            )
        except Exception as exc:  # noqa: BLE001 — session.update may raise on bad data
            return _envelope_failure(
                ErrorCode.INVALID_INPUT,
                f"could not persist selection: {exc}",
                status=500,
            )

        if confirm_event is not None and not confirm_event.is_set():
            confirm_event.set()

        return JSONResponse(
            {
                "success": True,
                "data": {
                    "queue": list(new_state.queue),
                    "mode": new_state.mode,
                },
                "error": None,
                "code": None,
            },
            status_code=200,
        )

    return confirm


# --------------------------------------------------------------------------
# Public factory
# --------------------------------------------------------------------------


def build_ui_app(
    app: GhiaApp,
    *,
    confirm_event: Optional[asyncio.Event] = None,
) -> Starlette:
    """Construct the picker Starlette app bound to ``app``.

    Args:
        app: The composition-root :class:`GhiaApp`.  Routes close over
            this instance, so a single sub-app serves exactly one
            agent.
        confirm_event: Optional :class:`asyncio.Event` set by the
            ``/api/confirm`` handler after a successful write.  Used
            by :mod:`ghia.ui.opener` to await user confirmation
            without polling.  ``None`` (the default) leaves no signal
            wired up — fine for tests and for callers that don't need
            it.

    Returns:
        A fully configured :class:`Starlette` app.  No middleware
        beyond what Starlette adds itself — the picker is local-only
        and we want to minimise attack surface.
    """

    routes = [
        Route("/", _make_index_handler(app), methods=["GET"]),
        Route(
            "/api/issues",
            _make_issues_handler(app),
            methods=["GET"],
        ),
        Route(
            "/api/confirm",
            _make_confirm_handler(app, confirm_event),
            methods=["POST"],
        ),
    ]

    # debug=False even in dev: a Starlette debug page would happily
    # render request data, and we'd rather see the structured error.
    return Starlette(debug=False, routes=routes)


def run_ui_server(
    app: GhiaApp,
    *,
    host: str = UI_BIND_HOST,
    port: int = 4242,
    confirm_event: Optional[asyncio.Event] = None,
) -> uvicorn.Server:
    """Build a configured :class:`uvicorn.Server` ready to ``serve()``.

    The caller decides when to start the server (so the opener can
    schedule it as an asyncio task, await an event, then shut it
    down).  We do NOT call ``serve()`` here — returning the
    pre-configured Server is enough for the opener and keeps the
    factory side-effect free.

    Args:
        app: The :class:`GhiaApp` to serve.
        host: Bind host.  Defaults to (and is enforced as) loopback;
            accepts an override for tests but :func:`run_ui_server`
            still raises if a non-loopback address is requested.
        port: TCP port.  Default ``4242`` matches the TRD spec.
        confirm_event: Forwarded to :func:`build_ui_app`.

    Returns:
        A :class:`uvicorn.Server` whose ``config.host`` is guaranteed
        to be a loopback address.

    Raises:
        ValueError: If ``host`` is not a loopback address (``127.x.x.x``,
            ``::1``, or ``localhost``).  Defends against accidental
            ``0.0.0.0`` binding in code paths we don't control.
    """

    if host not in ("127.0.0.1", "::1", "localhost") and not host.startswith(
        "127."
    ):
        raise ValueError(
            "UI server must bind to a loopback address; "
            f"refusing to bind to {host!r}"
        )

    starlette_app = build_ui_app(app, confirm_event=confirm_event)
    config = uvicorn.Config(
        starlette_app,
        host=host,
        port=port,
        log_level="warning",  # uvicorn's INFO is noisy; warnings are enough
        access_log=False,
        # Single worker is correct for a per-process picker; we don't
        # want uvicorn forking inside an MCP server.
        workers=1,
    )
    return uvicorn.Server(config)
