"""TRD-021-TEST — UI opener (headless detection + browser orchestration).

We patch ``os.environ`` (via ``monkeypatch``), ``shutil.which``,
``sys.platform``, and ``webbrowser.open`` to control the decision
inputs without touching the real environment.

The browser-path orchestration test patches :func:`run_ui_server` to
return a stub server (no real socket bind) and uses the
:class:`asyncio.Event` that ``open_picker`` creates internally — we
fire it from the test by short-circuiting the server's ``serve``
method.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ghia import redaction
from ghia.app import GhiaApp, create_app
from ghia.ui import opener as ui_opener


_FAKE_TOKEN = "ghp_" + "n" * 36
_REPO = "octo/hello"


@pytest.fixture(autouse=True)
def _reset_logging() -> None:
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


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every env var that influences :func:`is_headless`."""

    for var in (
        "GHIA_FORCE_BROWSER",
        "GHIA_FORCE_TERMINAL",
        "SSH_CONNECTION",
        "DISPLAY",
        "WAYLAND_DISPLAY",
    ):
        monkeypatch.delenv(var, raising=False)


# ----------------------------------------------------------------------
# is_headless — heuristic matrix
# ----------------------------------------------------------------------


def test_is_headless_false_when_display_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    # WAYLAND_DISPLAY intentionally empty.
    assert ui_opener.is_headless() is False


def test_is_headless_false_when_wayland_set_even_without_x(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wayland-only sessions have $DISPLAY empty but a usable browser."""

    _clear_env(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert ui_opener.is_headless() is False


def test_is_headless_false_when_xdg_open_present_even_without_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """xdg-open implies *some* opener is configured — trust it."""

    _clear_env(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ui_opener.shutil, "which", lambda _: "/usr/bin/xdg-open")
    assert ui_opener.is_headless() is False


def test_is_headless_true_on_linux_no_display_no_wayland_no_xdg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ui_opener.shutil, "which", lambda _: None)
    assert ui_opener.is_headless() is True


def test_is_headless_true_when_ssh_connection_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSH = headless even with X11 forwarding set up."""

    _clear_env(monkeypatch)
    monkeypatch.setenv("DISPLAY", ":10")  # forwarded
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 22 5.6.7.8 22")
    assert ui_opener.is_headless() is True


def test_force_terminal_overrides_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("GHIA_FORCE_TERMINAL", "1")
    assert ui_opener.is_headless() is True


def test_force_browser_overrides_no_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ui_opener.shutil, "which", lambda _: None)
    monkeypatch.setenv("GHIA_FORCE_BROWSER", "1")
    assert ui_opener.is_headless() is False


def test_force_browser_overrides_force_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If both are set, the more permissive override wins."""

    _clear_env(monkeypatch)
    monkeypatch.setenv("GHIA_FORCE_BROWSER", "1")
    monkeypatch.setenv("GHIA_FORCE_TERMINAL", "1")
    assert ui_opener.is_headless() is False


def test_force_browser_overrides_ssh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("SSH_CONNECTION", "x")
    monkeypatch.setenv("GHIA_FORCE_BROWSER", "1")
    assert ui_opener.is_headless() is False


def test_non_linux_default_is_not_headless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS / Windows fall through to ``False`` — webbrowser handles them."""

    _clear_env(monkeypatch)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert ui_opener.is_headless() is False


# ----------------------------------------------------------------------
# open_picker — orchestration
# ----------------------------------------------------------------------


async def test_open_picker_uses_terminal_when_headless(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Headless env routes straight to the terminal picker."""

    monkeypatch.setattr(ui_opener, "is_headless", lambda: True)

    called = {"count": 0}
    expected = {"queue": [42], "mode": "semi"}

    async def fake_terminal(_app):
        called["count"] += 1
        return expected

    # Patch on the symbol the lazy import resolves to.
    from ghia.ui import terminal as terminal_mod

    monkeypatch.setattr(terminal_mod, "pick_issues_terminal", fake_terminal)

    # Belt-and-braces: webbrowser must not be touched on the headless path.
    webbrowser_open = MagicMock()
    monkeypatch.setattr(ui_opener.webbrowser, "open", webbrowser_open)

    result = await ui_opener.open_picker(app)

    assert called["count"] == 1
    assert result == expected
    webbrowser_open.assert_not_called()


class _StubServer:
    """Stand-in for :class:`uvicorn.Server` — no socket, no event loop."""

    def __init__(self) -> None:
        self.config = MagicMock(host="127.0.0.1", port=4242)
        self.should_exit = False
        self._serve_done = asyncio.Event()
        self._cancelled = False

    async def serve(self) -> None:
        # Block until told to exit, just like uvicorn.Server.serve().
        await self._serve_done.wait()


async def test_open_picker_browser_path_invokes_webbrowser_open(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a display, open_picker fires webbrowser.open at localhost."""

    monkeypatch.setattr(ui_opener, "is_headless", lambda: False)

    captured_event_holder: dict[str, asyncio.Event] = {}
    stub = _StubServer()

    def fake_run_ui_server(_app, *, host, port, confirm_event):
        # Capture the event so the test can fire it from the side.
        captured_event_holder["event"] = confirm_event
        return stub

    from ghia.ui import server as server_mod

    monkeypatch.setattr(server_mod, "run_ui_server", fake_run_ui_server)

    webbrowser_open = MagicMock()
    monkeypatch.setattr(ui_opener.webbrowser, "open", webbrowser_open)

    # Pre-seed session so the post-confirm read returns recognisable data.
    await app.session.update(queue=[7, 9], mode="full")

    async def fire_after_delay() -> None:
        # Wait for the event to be created, then set it (simulates the
        # /api/confirm route running).  Also unblock the stub's serve.
        for _ in range(50):
            if "event" in captured_event_holder:
                break
            await asyncio.sleep(0.01)
        captured_event_holder["event"].set()
        stub._serve_done.set()

    fire_task = asyncio.create_task(fire_after_delay())
    try:
        result = await ui_opener.open_picker(app, port=4242, timeout_sec=2.0)
    finally:
        await fire_task

    webbrowser_open.assert_called_once_with("http://127.0.0.1:4242/")
    assert result == {"queue": [7, 9], "mode": "full"}
    assert stub.should_exit is True


async def test_open_picker_falls_back_to_terminal_on_timeout(
    app: GhiaApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the user never confirms, we drop to the terminal picker."""

    monkeypatch.setattr(ui_opener, "is_headless", lambda: False)

    stub = _StubServer()

    def fake_run_ui_server(_app, *, host, port, confirm_event):
        return stub

    from ghia.ui import server as server_mod
    from ghia.ui import terminal as terminal_mod

    monkeypatch.setattr(server_mod, "run_ui_server", fake_run_ui_server)
    monkeypatch.setattr(ui_opener.webbrowser, "open", MagicMock())

    fallback_called = {"count": 0}

    async def fake_terminal(_app):
        fallback_called["count"] += 1
        # Unblock the stub's serve so the finally clause exits cleanly.
        stub._serve_done.set()
        return {"queue": [], "mode": "semi"}

    monkeypatch.setattr(terminal_mod, "pick_issues_terminal", fake_terminal)

    # Tiny timeout so the test runs fast.
    result = await ui_opener.open_picker(app, port=4242, timeout_sec=0.05)

    assert fallback_called["count"] == 1
    assert result == {"queue": [], "mode": "semi"}
