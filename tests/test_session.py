"""TRD-006-TEST — verify SessionStore singleton.

Covers:
* Persistence across SessionStore reinstantiation (simulates process
  restart on the same file).
* Corrupted JSON rotates to ``.bak-{ts}`` and starts fresh.
* Concurrent ``update()`` calls serialize and all mutations are
  eventually visible.
* ``read()`` returns a snapshot that can't accidentally mutate the
  backing file when the caller modifies it.
* ``reset_to_idle`` clears runtime fields.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ghia.session import SessionState, SessionStore


async def test_default_state_is_idle(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.json")
    state = await store.read()
    assert state.status == "idle"
    assert state.mode == "semi"
    assert state.queue == []


async def test_update_persists_across_reinstantiation(tmp_path: Path) -> None:
    path = tmp_path / "session.json"

    store_a = SessionStore(path)
    await store_a.update(status="active", repo="octo/hello", queue=[1, 2, 3])

    # Simulate process restart — fresh store, same file.
    store_b = SessionStore(path)
    reloaded = await store_b.read()
    assert reloaded.status == "active"
    assert reloaded.repo == "octo/hello"
    assert reloaded.queue == [1, 2, 3]


async def test_corrupt_json_rotates_to_bak(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text("{not json at all")

    store = SessionStore(path)
    state = await store.read()

    # Fresh idle state was produced.
    assert state.status == "idle"
    assert state.queue == []

    # Backup file exists with the .bak- prefix.
    backups = [p for p in tmp_path.iterdir() if ".bak-" in p.name]
    assert backups, "corrupt file was not rotated"
    assert backups[0].read_text().startswith("{not json")


async def test_schema_invalid_json_rotates_to_bak(tmp_path: Path) -> None:
    """Well-formed JSON with wrong shape is treated like corruption."""
    path = tmp_path / "session.json"
    path.write_text('{"status": "bogus-state"}')  # not in the Literal set

    store = SessionStore(path)
    state = await store.read()
    assert state.status == "idle"

    backups = [p for p in tmp_path.iterdir() if ".bak-" in p.name]
    assert backups


async def test_update_rejects_bad_field(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.json")
    with pytest.raises(Exception):  # pydantic ValidationError subclass
        await store.update(status="nonsense")  # type: ignore[arg-type]


async def test_concurrent_updates_serialize(tmp_path: Path) -> None:
    """Many concurrent updates must not lose writes."""
    store = SessionStore(tmp_path / "session.json")

    # Seed with an empty queue, then append under the shared lock.
    await store.update(queue=[])

    async def append(n: int) -> None:
        async with store.lock:
            current = await store.read()
            new_queue = [*current.queue, n]
            # Can't call store.update while holding the lock (reentrant
            # lock would deadlock); write directly through the private
            # helper the way update() does, under the held lock.
            store._persist(SessionState.model_validate({
                **current.model_dump(),
                "queue": new_queue,
            }))

    await asyncio.gather(*(append(i) for i in range(10)))

    final = await store.read()
    assert sorted(final.queue) == list(range(10))


async def test_read_snapshot_is_independent(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.json")
    await store.update(queue=[1, 2])

    snap = await store.read()
    snap.queue.append(99)  # mutate local copy

    reloaded = await store.read()
    assert reloaded.queue == [1, 2], "local mutation leaked into store"


async def test_reset_to_idle_clears_state(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.json")
    await store.update(
        status="active",
        queue=[1, 2, 3],
        active_issue=1,
        repo="octo/hello",
    )

    await store.reset_to_idle()

    state = await store.read()
    assert state.status == "idle"
    assert state.queue == []
    assert state.active_issue is None
    assert state.repo is None


async def test_on_disk_file_is_valid_json(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    store = SessionStore(path)
    await store.update(mode="full", queue=[7])

    data = json.loads(path.read_text())
    assert data["mode"] == "full"
    assert data["queue"] == [7]
