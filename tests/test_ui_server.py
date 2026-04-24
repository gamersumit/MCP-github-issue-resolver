"""TRD-018-TEST — Starlette picker sub-app.

Tests drive the ASGI app directly via ``httpx.AsyncClient(transport=
ASGITransport(app=...))`` rather than starting a real uvicorn server.
This keeps the suite fast, deterministic, and free of port-allocation
flakes — and it tests the same code path the running server hits.

We also assert the loopback-bind invariant on the
:class:`uvicorn.Server` config produced by :func:`run_ui_server`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest

from ghia import redaction
from ghia.app import GhiaApp, create_app
from ghia.errors import ErrorCode, ok
from ghia.tools import issues as issue_tools
from ghia.ui import server as ui_server


_FAKE_TOKEN = "ghp_" + "z" * 36
_REPO = "octo/hello"


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
    """Strip filters left by other modules so tests don't cross-pollute."""

    root = logging.getLogger()
    before = list(root.filters)
    redaction.set_token(None)
    yield
    for f in list(root.filters):
        if f not in before:
            root.removeFilter(f)
    redaction.set_token(None)


def _write_config(path: Path, **overrides: Any) -> None:
    payload: dict[str, Any] = {
        "token": _FAKE_TOKEN,
        "repo": _REPO,
        "label": "ai-fix",
        "mode": "semi",
        "poll_interval_min": 30,
    }
    payload.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


@pytest.fixture
async def app(tmp_path: Path) -> GhiaApp:
    cfg_path = tmp_path / "cfg.json"
    _write_config(cfg_path)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    return await create_app(repo_root=repo_root, config_path=cfg_path)


@pytest.fixture
async def client(app: GhiaApp) -> AsyncIterator[httpx.AsyncClient]:
    """An AsyncClient wired to the picker ASGI app."""

    starlette_app = ui_server.build_ui_app(app)
    transport = httpx.ASGITransport(app=starlette_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        yield c


# ----------------------------------------------------------------------
# GET /api/issues
# ----------------------------------------------------------------------


async def test_get_issues_returns_envelope_with_data(
    app: GhiaApp,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The /api/issues route returns the ToolResponse envelope verbatim."""

    sample = [
        {
            "number": 1,
            "title": "fix the bug",
            "body": "",
            "labels": ["bug"],
            "html_url": "https://github.com/octo/hello/issues/1",
            "created_at": "2026-04-01T00:00:00Z",
            "updated_at": "2026-04-02T00:00:00Z",
            "author": "alice",
            "assignees": [],
            "comments_count": 0,
            "priority": "high",
        }
    ]

    async def fake_list_issues(_app, label=None):
        return ok({"issues": sample, "count": 1})

    monkeypatch.setattr(ui_server, "list_issues", fake_list_issues)

    resp = await client.get("/api/issues")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["error"] is None
    assert body["code"] is None
    assert body["data"]["count"] == 1
    assert body["data"]["issues"][0]["number"] == 1


async def test_get_issues_propagates_tool_failure_as_500(
    app: GhiaApp,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool-layer failures still produce the structured envelope."""

    from ghia.errors import err

    async def failing(_app, label=None):
        return err(ErrorCode.RATE_LIMITED, "no quota")

    monkeypatch.setattr(ui_server, "list_issues", failing)

    resp = await client.get("/api/issues")
    assert resp.status_code == 500
    body = resp.json()
    assert body["success"] is False
    assert body["code"] == "RATE_LIMITED"
    assert "no quota" in body["error"]


# ----------------------------------------------------------------------
# POST /api/confirm
# ----------------------------------------------------------------------


async def test_post_confirm_writes_session_and_returns_envelope(
    app: GhiaApp, client: httpx.AsyncClient
) -> None:
    """A valid confirm payload persists ``queue`` + ``mode`` to SessionStore."""

    resp = await client.post(
        "/api/confirm",
        json={"queue": [3, 1, 2], "mode": "full"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["queue"] == [3, 1, 2]
    assert body["data"]["mode"] == "full"

    # Verify the write actually landed in SessionStore.
    state = await app.session.read()
    assert state.queue == [3, 1, 2]
    assert state.mode == "full"


async def test_post_confirm_dedupes_queue_preserving_order(
    app: GhiaApp, client: httpx.AsyncClient
) -> None:
    resp = await client.post(
        "/api/confirm",
        json={"queue": [3, 1, 3, 2, 1], "mode": "semi"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["queue"] == [3, 1, 2]


async def test_post_confirm_invalid_mode_returns_400(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/api/confirm",
        json={"queue": [1], "mode": "bogus"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["code"] == "INVALID_INPUT"
    assert "mode" in body["error"]


async def test_post_confirm_negative_issue_number_returns_400(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/api/confirm",
        json={"queue": [-1, 2], "mode": "semi"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["code"] == "INVALID_INPUT"


async def test_post_confirm_missing_mode_returns_400(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/api/confirm",
        json={"queue": [1]},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_INPUT"


async def test_post_confirm_invalid_json_returns_400(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/api/confirm",
        content=b"not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_INPUT"


async def test_post_confirm_sets_event_when_provided(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opener uses an Event to await user confirmation; verify it fires."""

    event = asyncio.Event()
    starlette_app = ui_server.build_ui_app(app, confirm_event=event)
    transport = httpx.ASGITransport(app=starlette_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        assert not event.is_set()
        resp = await c.post(
            "/api/confirm",
            json={"queue": [5], "mode": "semi"},
        )
    assert resp.status_code == 200
    assert event.is_set()


# ----------------------------------------------------------------------
# GET / (picker.html)
# ----------------------------------------------------------------------


async def test_get_index_serves_picker_html(
    client: httpx.AsyncClient,
) -> None:
    """The static asset is served verbatim with text/html content-type."""

    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    text = resp.text
    # Smoke-check a few load-bearing strings from picker.html.
    assert "github-issue-agent" in text
    assert "/api/issues" in text
    assert "/api/confirm" in text


async def test_get_index_503_when_picker_html_missing(
    app: GhiaApp,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A missing static asset surfaces as a structured 503, not a stack trace."""

    missing = tmp_path / "nope.html"
    monkeypatch.setattr(ui_server, "picker_html_path", lambda: missing)

    resp = await client.get("/")
    assert resp.status_code == 503
    body = resp.json()
    assert body["success"] is False
    assert body["code"] == "FILE_NOT_FOUND"


async def test_picker_html_is_self_contained() -> None:
    """No external <script src=> or <link href=> with off-host URLs.

    This is the smoke-test for TRD-019a's "self-contained" rule, in
    lieu of a Playwright check (TRD-INFRA-02).  We accept any on-host
    or relative URL but reject anything pointing at a remote origin.
    """

    path = ui_server.picker_html_path()
    assert path.is_file(), f"picker.html missing at {path}"
    text = path.read_text(encoding="utf-8")

    import re

    for tag in re.findall(r'<script[^>]*\bsrc=["\']([^"\']+)["\']', text):
        assert (
            "://" not in tag and not tag.startswith("//")
        ), f"external script src is forbidden: {tag!r}"
    for tag in re.findall(r'<link[^>]*\bhref=["\']([^"\']+)["\']', text):
        assert (
            "://" not in tag and not tag.startswith("//")
        ), f"external link href is forbidden: {tag!r}"
    # Picker must declare both prefers-color-scheme palettes.
    assert "prefers-color-scheme: dark" in text


# ----------------------------------------------------------------------
# run_ui_server config — loopback bind invariant
# ----------------------------------------------------------------------


async def test_run_ui_server_binds_loopback_only(app: GhiaApp) -> None:
    """The configured uvicorn Server must bind 127.0.0.1, not 0.0.0.0."""

    server = ui_server.run_ui_server(app)
    assert server.config.host == "127.0.0.1"
    assert server.config.port == 4242


async def test_run_ui_server_refuses_non_loopback_host(app: GhiaApp) -> None:
    """Defence-in-depth: an explicit external bind must raise."""

    with pytest.raises(ValueError, match="loopback"):
        ui_server.run_ui_server(app, host="0.0.0.0")
    with pytest.raises(ValueError, match="loopback"):
        ui_server.run_ui_server(app, host="192.168.1.1")


async def test_run_ui_server_accepts_localhost_alias(app: GhiaApp) -> None:
    server = ui_server.run_ui_server(app, host="localhost")
    assert server.config.host == "localhost"


# ----------------------------------------------------------------------
# Static path resolves under repo root
# ----------------------------------------------------------------------


def test_picker_html_path_points_at_ui_static() -> None:
    """``picker_html_path`` resolves to ``<repo_root>/ui_static/picker.html``."""

    p = ui_server.picker_html_path()
    assert p.name == "picker.html"
    assert p.parent.name == "ui_static"
