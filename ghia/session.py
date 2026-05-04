"""SessionStore — singleton runtime-state holder (TRD-006).

SessionStore is the single writer of ``session.json``.  All mutations
go through an ``asyncio.Lock`` to serialize concurrent tool calls,
polling ticks, and UI confirmations.  Reads are lock-free snapshots
that load the current on-disk state and return an immutable-by-copy
:class:`SessionState`.

If ``session.json`` is corrupted on load we rotate it to
``session.json.bak-{iso_ts}`` and start fresh — this matches the TRD's
"corrupted → rotate, start fresh" policy and ensures a bad file never
wedges the agent on startup.

Satisfies REQ-006.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ghia.atomic import atomic_write_text

logger = logging.getLogger(__name__)

__all__ = ["SessionState", "SessionStore"]


class SessionState(BaseModel):
    """Persistent runtime state for the agent.

    A snapshot of this model is what ``session.json`` serializes to.
    All fields have conservative defaults so an empty / fresh store is
    a valid ``idle`` session.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["idle", "active"] = "idle"
    mode: Literal["semi", "full"] = "semi"
    repo: Optional[str] = None
    queue: list[int] = Field(default_factory=list)
    active_issue: Optional[int] = None
    completed: list[int] = Field(default_factory=list)
    skipped: list[int] = Field(default_factory=list)
    poll_timer_active: bool = False
    last_fetched: Optional[datetime] = None
    session_started: Optional[datetime] = None
    # When the user (or a tool) last paused the agent. Preserved
    # across stop so a subsequent start can detect "we have work in
    # flight, resume mid-issue rather than starting fresh".
    paused_at: Optional[datetime] = None
    default_branch: Optional[str] = None
    discovered_conventions: Optional[str] = None


def _timestamp_for_backup() -> str:
    """ISO-8601 UTC timestamp suitable for a filename suffix.

    We replace ``:`` with ``-`` so the path is portable to filesystems
    that dislike colons (Windows, anything FAT-ish).
    """

    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace(":", "-")
    )


class SessionStore:
    """Async-safe session state store backed by an atomic JSON file.

    Usage:

    * ``await store.read()`` → snapshot (no lock; cheap).
    * ``await store.update(status="active")`` → guarded write.
    * ``async with store.lock:`` → caller-held lock for compound
      read-modify-write ops that must span multiple statements.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(os.fspath(path))
        # Public so other modules can use it directly for compound ops.
        self.lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_or_recover(self) -> SessionState:
        """Return the current on-disk state, rotating bad files aside.

        Contract:
            * No file → fresh default state (no file written yet).
            * File + valid → parsed :class:`SessionState`.
            * File + invalid (bad JSON or bad schema) → the bad file is
              renamed to ``{path}.bak-{iso_ts}`` and a fresh default is
              returned.  A WARNING is logged with the original path so
              operators can investigate.
        """

        if not self.path.exists():
            return SessionState()

        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "could not read session file %s: %s; rotating and starting fresh",
                self.path,
                exc,
            )
            self._rotate_corrupt()
            return SessionState()

        try:
            data: Any = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("session file must be a JSON object")
            return SessionState.model_validate(data)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            logger.warning(
                "session file %s is corrupt (%s); rotating to .bak and starting fresh",
                self.path,
                exc,
            )
            self._rotate_corrupt()
            return SessionState()

    def _rotate_corrupt(self) -> None:
        """Move the current (corrupt) file aside.  Best-effort."""

        if not self.path.exists():
            return
        backup = self.path.with_name(
            f"{self.path.name}.bak-{_timestamp_for_backup()}"
        )
        try:
            os.replace(self.path, backup)
            logger.info("rotated corrupt session file to %s", backup)
        except OSError as exc:
            logger.warning(
                "failed to rotate corrupt session file %s: %s", self.path, exc
            )

    def _persist(self, state: SessionState) -> None:
        """Atomically write ``state`` to :attr:`path`."""

        payload = state.model_dump_json(indent=2)
        atomic_write_text(self.path, payload + "\n")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def read(self) -> SessionState:
        """Return a snapshot of current on-disk state.

        No lock is taken — each call loads and parses the file.  The
        returned model is a fresh instance, so callers mutating it do
        not affect other readers or any subsequent load.
        """

        # Load is cheap (tens of KB at most) and running it unlocked is
        # safe because SessionStore is the only writer and writes are
        # atomic (os.replace).
        return self._load_or_recover()

    async def update(self, **kwargs: Any) -> SessionState:
        """Apply ``kwargs`` to current state under the store lock.

        Returns the new state.  Keys must correspond to valid
        :class:`SessionState` fields; Pydantic raises
        ``ValidationError`` otherwise (propagated to caller).
        """

        async with self.lock:
            current = self._load_or_recover()
            # model_copy with update gives us a new, revalidated instance.
            # We go through model_validate to catch bad values on fields
            # that have validators (model_copy does a shallow replace).
            merged = {**current.model_dump(), **kwargs}
            new_state = SessionState.model_validate(merged)
            self._persist(new_state)
            return new_state

    async def reset_to_idle(self) -> None:
        """Clear runtime fields back to a clean idle state.

        Convenience for ``issue_agent_stop``.  Preserves nothing — the
        caller is responsible for capturing anything worth keeping
        before calling this.
        """

        async with self.lock:
            fresh = SessionState()
            self._persist(fresh)
