"""Tests for ghia.queue_processor (TRD-029-TEST)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator, List

import pytest

from ghia.app import GhiaApp, create_app
from ghia.errors import ErrorCode, ToolResponse, err, ok
from ghia.queue_processor import process_queue


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _write_config(path: Path) -> None:
    payload: dict[str, Any] = {
        "token": "ghp_" + "q" * 36,
        "repo": "octo/queue",
        "label": "ai-fix",
        "mode": "semi",
        "poll_interval_min": 30,
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
    yield instance


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


async def test_process_queue_drains_all_completed(app: GhiaApp) -> None:
    await app.session.update(queue=[1, 2, 3])

    async def handler(_app: GhiaApp, _n: int) -> ToolResponse:
        return ok({"completed": True})

    result = await process_queue(app, handler=handler)

    assert result["processed"] == [1, 2, 3]
    assert result["skipped"] == []
    assert result["remaining"] == []
    assert result["paused"] is False

    state = await app.session.read()
    assert state.queue == []
    assert state.completed == [1, 2, 3]
    assert state.active_issue is None


async def test_handler_called_in_order(app: GhiaApp) -> None:
    await app.session.update(queue=[42, 7, 99])
    seen: List[int] = []

    async def handler(_app: GhiaApp, n: int) -> ToolResponse:
        seen.append(n)
        return ok({"completed": True})

    await process_queue(app, handler=handler)
    assert seen == [42, 7, 99]


# ----------------------------------------------------------------------
# Pause behavior (NETWORK_ERROR / RATE_LIMITED)
# ----------------------------------------------------------------------


async def test_network_error_pauses_and_preserves_queue(app: GhiaApp) -> None:
    await app.session.update(queue=[1, 2, 3])

    async def handler(_app: GhiaApp, n: int) -> ToolResponse:
        if n == 2:
            return err(ErrorCode.NETWORK_ERROR, "GitHub unreachable")
        return ok({"completed": True})

    result = await process_queue(app, handler=handler)

    assert result["processed"] == [1]
    assert result["paused"] is True
    assert result["reason"] == "NETWORK_ERROR"
    # Issue 2 stays at the head of the queue along with anything after it.
    assert result["remaining"] == [2, 3]

    state = await app.session.read()
    assert state.queue == [2, 3]
    assert state.completed == [1]
    # active_issue must be cleared so a resume re-picks issue 2.
    assert state.active_issue is None


async def test_rate_limited_also_pauses(app: GhiaApp) -> None:
    await app.session.update(queue=[10, 20])

    async def handler(_app: GhiaApp, _n: int) -> ToolResponse:
        return err(ErrorCode.RATE_LIMITED, "quota exceeded")

    result = await process_queue(app, handler=handler)

    assert result["paused"] is True
    assert result["reason"] == "RATE_LIMITED"
    assert result["processed"] == []
    state = await app.session.read()
    assert state.queue == [10, 20]
    assert state.active_issue is None


# ----------------------------------------------------------------------
# Skip on logical failure
# ----------------------------------------------------------------------


async def test_invalid_input_skips_and_continues(app: GhiaApp) -> None:
    await app.session.update(queue=[1, 2, 3])

    async def handler(_app: GhiaApp, n: int) -> ToolResponse:
        if n == 2:
            return err(ErrorCode.INVALID_INPUT, "issue 2 is malformed")
        return ok({"completed": True})

    result = await process_queue(app, handler=handler)

    assert result["processed"] == [1, 3]
    assert result["skipped"] == [2]
    assert result["paused"] is False

    state = await app.session.read()
    assert state.queue == []
    assert state.completed == [1, 3]
    assert state.skipped == [2]


# ----------------------------------------------------------------------
# Long-queue warning
# ----------------------------------------------------------------------


async def test_long_queue_emits_warning(
    app: GhiaApp,
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue = list(range(1, 12))  # 11 items > threshold of 10
    await app.session.update(queue=queue)

    async def handler(_app: GhiaApp, _n: int) -> ToolResponse:
        return ok({"completed": True})

    with caplog.at_level(logging.WARNING, logger="ghia"):
        result = await process_queue(app, handler=handler)

    # All 11 still processed; the warning is informational only.
    assert result["processed"] == queue

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("queue has 11 items" in w for w in warnings), warnings


async def test_short_queue_emits_no_warning(
    app: GhiaApp,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await app.session.update(queue=[1, 2, 3])

    async def handler(_app: GhiaApp, _n: int) -> ToolResponse:
        return ok({"completed": True})

    with caplog.at_level(logging.WARNING, logger="ghia"):
        await process_queue(app, handler=handler)

    warnings = [r.getMessage() for r in caplog.records if "queue has" in r.getMessage()]
    assert warnings == []


# ----------------------------------------------------------------------
# Default handler
# ----------------------------------------------------------------------


async def test_default_handler_marks_completed(app: GhiaApp) -> None:
    """When no handler is supplied, the stub auto-completes each issue."""

    await app.session.update(queue=[5, 6])

    result = await process_queue(app)

    assert result["processed"] == [5, 6]
    state = await app.session.read()
    assert state.completed == [5, 6]
    assert state.queue == []
