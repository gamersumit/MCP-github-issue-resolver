"""Control-plane tests (TRD-011-TEST + TRD-012-TEST).

Covers:
* start from idle → status becomes active, protocol is non-empty,
  polling task is created
* start from active → INVALID_INPUT error
* stop from active → status becomes idle, polling task is cancelled
* set_mode("full") → SessionState.mode == "full"
* set_mode("invalid") → INVALID_INPUT error
* Mid-session: start → set_mode → status reflects new mode immediately
* fetch_now triggers a tick and updates last_fetched
* status returns structured SessionState echo
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from ghia import polling, redaction
from ghia.app import GhiaApp, create_app
from ghia.errors import ErrorCode
from ghia.tools import control


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_logging_filters() -> None:
    """Remove any RedactionFilter the test leaks onto the root logger."""

    root = logging.getLogger()
    before = list(root.filters)
    redaction.set_token(None)
    yield
    for f in list(root.filters):
        if f not in before:
            root.removeFilter(f)
    redaction.set_token(None)


@pytest.fixture(autouse=True)
def _stub_polling_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``polling._tick_once`` with a no-network stub.

    Without this, every ``issue_agent_start`` would spawn a polling
    task that immediately tries to shell out to ``gh``.  The stub
    keeps the lifecycle wiring honest (start_polling and stop_polling
    still run) without making the test suite network-dependent.
    """

    async def _no_network(app: GhiaApp) -> None:
        await app.session.update(
            last_fetched=datetime.now(tz=timezone.utc)
        )

    monkeypatch.setattr(polling, "_tick_once", _no_network)


def _write_config(path: Path, **overrides: Any) -> None:
    """v0.2 per-repo config — no token, no repo field."""

    payload: dict[str, Any] = {
        "label": "ai-fix",
        "mode": "semi",
        "poll_interval_min": 30,
    }
    payload.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[GhiaApp]:
    """A fully-wired GhiaApp rooted at ``tmp_path``.

    Tears down any background polling task on exit so a test that
    starts the agent doesn't leak a poller into the next test's event
    loop.
    """

    cfg_path = tmp_path / "cfg.json"
    _write_config(cfg_path)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    instance = await create_app(
        repo_root=repo_root, config_path=cfg_path, repo_full_name="octo/hello"
    )
    try:
        yield instance
    finally:
        await polling.stop_polling(instance)


# ----------------------------------------------------------------------
# start / stop
# ----------------------------------------------------------------------


async def test_start_from_idle_activates(app: GhiaApp) -> None:
    resp = await control.issue_agent_start(app)

    assert resp.success, resp.error
    assert resp.data is not None
    protocol = resp.data["protocol"]
    assert isinstance(protocol, str) and protocol
    # Protocol must reflect the configured repo.
    assert "octo/hello" in protocol

    state = await app.session.read()
    assert state.status == "active"
    assert state.repo == "octo/hello"
    assert state.session_started is not None


async def test_start_when_already_active_refreshes(app: GhiaApp) -> None:
    """Re-calling start while active is now idempotent (v0.2.1+).

    The user-facing motivation: after editing the wizard config to
    switch mode/labels, the natural call is ``start`` again — a hard
    error here was an unhelpful footgun that forced a stop+start
    dance. The re-call now refreshes config-derived state in place,
    flagged via ``refreshed=True`` so the LLM can phrase its
    announcement accordingly. ``session_started`` is preserved so the
    "how long has this been running" timer doesn't reset on refresh.
    """

    first = await control.issue_agent_start(app)
    assert first.success
    assert first.data["refreshed"] is False
    original_started = first.data["session_started"]

    second = await control.issue_agent_start(app)
    assert second.success
    assert second.data["refreshed"] is True
    # session_started must not reset on refresh.
    assert second.data["session_started"] == original_started


async def test_stop_from_active_returns_to_idle(app: GhiaApp) -> None:
    await control.issue_agent_start(app)
    # Simulate some progress so the summary has something to report.
    await app.session.update(completed=[1, 2], skipped=[3])

    resp = await control.issue_agent_stop(app)
    assert resp.success
    assert resp.data["completed_count"] == 2
    assert resp.data["skipped_count"] == 1
    assert "2 issues completed" in resp.data["message"]

    state = await app.session.read()
    assert state.status == "idle"
    assert state.active_issue is None
    # History preserved.
    assert state.completed == [1, 2]
    assert state.skipped == [3]


async def test_stop_from_idle_is_safe(app: GhiaApp) -> None:
    """Stop is idempotent — calling it from idle doesn't error."""

    resp = await control.issue_agent_stop(app)
    assert resp.success
    state = await app.session.read()
    assert state.status == "idle"


async def test_start_creates_polling_task(app: GhiaApp) -> None:
    """issue_agent_start spawns the background poller."""

    assert app._polling_task is None
    resp = await control.issue_agent_start(app)
    assert resp.success

    assert app._polling_task is not None
    assert not app._polling_task.done()

    state = await app.session.read()
    assert state.poll_timer_active is True


async def test_stop_cancels_polling_task(app: GhiaApp) -> None:
    """issue_agent_stop cancels the poller and clears the handle."""

    await control.issue_agent_start(app)
    task = app._polling_task
    assert task is not None

    resp = await control.issue_agent_stop(app)
    assert resp.success
    assert app._polling_task is None
    assert task.done()  # cancelled and awaited

    state = await app.session.read()
    assert state.poll_timer_active is False


# ----------------------------------------------------------------------
# status
# ----------------------------------------------------------------------


async def test_status_echoes_session_state(app: GhiaApp) -> None:
    await app.session.update(mode="full", queue=[1, 2, 3])
    resp = await control.issue_agent_status(app)

    assert resp.success
    data = resp.data
    assert data["status"] == "idle"
    assert data["mode"] == "full"
    assert data["queue"] == [1, 2, 3]
    assert "summary" in data
    assert "queue=3" in data["summary"]


async def test_status_after_start_shows_active(app: GhiaApp) -> None:
    await control.issue_agent_start(app)
    resp = await control.issue_agent_status(app)
    assert resp.success
    assert resp.data["status"] == "active"
    assert "active" in resp.data["summary"]


# ----------------------------------------------------------------------
# set_mode
# ----------------------------------------------------------------------


async def test_set_mode_to_full_persists(app: GhiaApp) -> None:
    resp = await control.issue_agent_set_mode(app, "full")
    assert resp.success
    assert resp.data["mode"] == "full"

    state = await app.session.read()
    assert state.mode == "full"


async def test_set_mode_to_semi_persists(app: GhiaApp) -> None:
    await control.issue_agent_set_mode(app, "full")
    resp = await control.issue_agent_set_mode(app, "semi")
    assert resp.success

    state = await app.session.read()
    assert state.mode == "semi"


async def test_set_mode_invalid_returns_error(app: GhiaApp) -> None:
    resp = await control.issue_agent_set_mode(app, "bananas")
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT


async def test_set_mode_empty_string_returns_error(app: GhiaApp) -> None:
    resp = await control.issue_agent_set_mode(app, "")
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT


# ----------------------------------------------------------------------
# Mid-session mode change (AC-007-3/4/5)
# ----------------------------------------------------------------------


async def test_mid_session_mode_change_visible_immediately(app: GhiaApp) -> None:
    await control.issue_agent_start(app)

    # Flip the mode while active.
    resp = await control.issue_agent_set_mode(app, "full")
    assert resp.success

    # Status must see the new mode on the very next call, without any
    # restart / re-fetch dance.
    status = await control.issue_agent_status(app)
    assert status.success
    assert status.data["mode"] == "full"
    assert status.data["status"] == "active"


# ----------------------------------------------------------------------
# fetch_now stub
# ----------------------------------------------------------------------


async def test_fetch_now_triggers_tick(app: GhiaApp) -> None:
    """fetch_now runs one polling tick and updates last_fetched."""

    resp = await control.issue_agent_fetch_now(app)
    assert resp.success, resp.error
    # last_fetched is populated by the (stubbed) tick.
    assert resp.data["last_fetched"] is not None

    state = await app.session.read()
    assert state.last_fetched is not None


# ----------------------------------------------------------------------
# Protocol-shape smoke tests
# ----------------------------------------------------------------------


async def test_start_protocol_contains_both_mode_neutral_sections(app: GhiaApp) -> None:
    resp = await control.issue_agent_start(app)
    assert resp.success
    protocol: str = resp.data["protocol"]
    # Rules, Naming, Mode changes, Error handling are mode-independent.
    assert "## Rules (both modes)" in protocol
    assert "## Naming" in protocol
    assert "## Error handling" in protocol
    # Only the semi arm should be present (config default).
    assert "SEMI-AUTO mode" in protocol
    assert "FULL-AUTO mode" not in protocol


async def test_start_after_mode_change_renders_full_arm(app: GhiaApp) -> None:
    await control.issue_agent_set_mode(app, "full")
    resp = await control.issue_agent_start(app)
    assert resp.success
    protocol: str = resp.data["protocol"]
    assert "FULL-AUTO mode" in protocol
    assert "SEMI-AUTO mode" not in protocol


async def test_start_includes_discovered_conventions_preview(
    tmp_path: Path,
) -> None:
    """Drop a CLAUDE.md into the repo root; preview must surface its content."""

    cfg_path = tmp_path / "cfg.json"
    _write_config(cfg_path)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "CLAUDE.md").write_text(
        "# Rules\n\nBe concise.\n"
    )

    app = await create_app(
        repo_root=repo_root, config_path=cfg_path, repo_full_name="octo/hello"
    )
    try:
        resp = await control.issue_agent_start(app)
        assert resp.success
        preview: str = resp.data["discovered_conventions_preview"]
        assert "Be concise" in preview or "Rules" in preview
        # Preview is capped at 200 chars.
        assert len(preview) <= 200
    finally:
        # Tear the poller down — the inline app skips the autouse fixture's
        # cleanup hook because it builds its own GhiaApp.
        await polling.stop_polling(app)
