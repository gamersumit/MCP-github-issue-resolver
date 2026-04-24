"""Tests for ghia.polling — background poller lifecycle (TRD-030-TEST)."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from ghia import polling
from ghia.app import GhiaApp, create_app


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _write_config(path: Path) -> None:
    payload: dict[str, Any] = {
        "token": "ghp_" + "p" * 36,
        "repo": "octo/poll",
        "label": "ai-fix",
        "mode": "semi",
        "poll_interval_min": 5,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[GhiaApp]:
    cfg_path = tmp_path / "cfg.json"
    _write_config(cfg_path)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    instance = await create_app(repo_root=repo_root, config_path=cfg_path)
    try:
        yield instance
    finally:
        await polling.stop_polling(instance)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace asyncio.sleep inside ghia.polling with an immediate yield.

    The polling loop sleeps ``poll_interval_min * 60`` seconds between
    ticks (>= 300s).  Patching it to a zero-yield lets the loop iterate
    deterministically inside a test without a 5-minute wait.

    We snapshot the real ``asyncio.sleep`` first and call THAT inside
    the stub — otherwise we'd recurse into the patched version.
    """

    real_sleep = asyncio.sleep

    async def _instant(_seconds: float) -> None:
        # Yield to the event loop so cancellation can be observed,
        # using the real sleep we captured before patching.
        await real_sleep(0)

    monkeypatch.setattr(polling.asyncio, "sleep", _instant)


# ----------------------------------------------------------------------
# start_polling / stop_polling
# ----------------------------------------------------------------------


async def test_start_polling_creates_task_and_writes_flag(app: GhiaApp) -> None:
    # Use a tick that immediately cancels itself so the loop doesn't
    # spin forever inside this assertion.
    ticks: list[int] = []

    async def on_tick(_app: GhiaApp) -> None:
        ticks.append(1)
        raise asyncio.CancelledError()

    # We need to bypass the default tick — start_polling only creates
    # the task, so swap _tick_once before starting.
    import ghia.polling as poll_mod

    poll_mod._tick_once = on_tick  # type: ignore[assignment]

    task = await polling.start_polling(app)
    try:
        assert app._polling_task is task
        assert isinstance(task, asyncio.Task)

        state = await app.session.read()
        assert state.poll_timer_active is True

        # Let the task run; it cancels itself on the first tick.
        with pytest.raises((asyncio.CancelledError, BaseException)):
            await asyncio.wait_for(task, timeout=1.0)
    finally:
        await polling.stop_polling(app)

    assert app._polling_task is None
    final = await app.session.read()
    assert final.poll_timer_active is False


async def test_stop_polling_is_safe_when_no_task(app: GhiaApp) -> None:
    """Calling stop without a running task is a no-op."""

    assert app._polling_task is None
    await polling.stop_polling(app)  # must not raise
    state = await app.session.read()
    assert state.poll_timer_active is False


async def test_stop_polling_cancels_running_task(app: GhiaApp) -> None:
    counter = {"n": 0}

    async def on_tick(_app: GhiaApp) -> None:
        counter["n"] += 1

    import ghia.polling as poll_mod

    poll_mod._tick_once = on_tick  # type: ignore[assignment]

    task = await polling.start_polling(app)
    # Yield enough times for at least one tick to fire.
    for _ in range(5):
        await asyncio.sleep(0)
    assert counter["n"] >= 1

    await polling.stop_polling(app)
    assert task.done()
    assert app._polling_task is None


# ----------------------------------------------------------------------
# polling_loop directly (no start/stop wrapper)
# ----------------------------------------------------------------------


async def test_polling_loop_calls_on_tick_each_iteration(app: GhiaApp) -> None:
    counter = {"n": 0}

    async def on_tick(_app: GhiaApp) -> None:
        counter["n"] += 1
        if counter["n"] >= 3:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await polling.polling_loop(app, on_tick=on_tick)

    assert counter["n"] == 3


async def test_polling_loop_continues_after_failing_tick(app: GhiaApp) -> None:
    """A handler raising a generic exception must not crash the loop."""

    counter = {"n": 0}

    async def on_tick(_app: GhiaApp) -> None:
        counter["n"] += 1
        if counter["n"] == 1:
            raise RuntimeError("simulated transient failure")
        if counter["n"] >= 3:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await polling.polling_loop(app, on_tick=on_tick)

    # The failing tick was tick #1; the loop kept going to ticks 2 and 3.
    assert counter["n"] == 3


async def test_polling_loop_logs_warning_on_failed_tick(
    app: GhiaApp,
    caplog: pytest.LogCaptureFixture,
) -> None:
    counter = {"n": 0}

    async def on_tick(_app: GhiaApp) -> None:
        counter["n"] += 1
        if counter["n"] == 1:
            raise RuntimeError("kaboom")
        raise asyncio.CancelledError()

    with caplog.at_level(logging.WARNING, logger="ghia"):
        with pytest.raises(asyncio.CancelledError):
            await polling.polling_loop(app, on_tick=on_tick)

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("poll tick failed" in w and "kaboom" in w for w in warnings), warnings


async def test_polling_loop_clears_flag_on_cancel(app: GhiaApp) -> None:
    """The CancelledError exit path must flip poll_timer_active off."""

    await app.session.update(poll_timer_active=True)

    async def on_tick(_app: GhiaApp) -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await polling.polling_loop(app, on_tick=on_tick)

    state = await app.session.read()
    assert state.poll_timer_active is False


# ----------------------------------------------------------------------
# stop_polling swallows the task's CancelledError
# ----------------------------------------------------------------------


async def test_stop_polling_does_not_propagate_cancelled_error(app: GhiaApp) -> None:
    async def on_tick(_app: GhiaApp) -> None:
        # Long-running tick that doesn't cancel itself; relies on
        # stop_polling to cancel it externally.
        await asyncio.sleep(60)

    import ghia.polling as poll_mod

    poll_mod._tick_once = on_tick  # type: ignore[assignment]

    await polling.start_polling(app)
    # stop_polling must not raise even though the underlying task is
    # cancelled mid-await.
    await polling.stop_polling(app)
    assert app._polling_task is None
