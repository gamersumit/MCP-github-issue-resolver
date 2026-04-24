"""Composition root (TRD-011 INFRA).

:class:`GhiaApp` bundles the dependencies a tool handler needs:
configuration, session store, logger, and the repo root.  Tool modules
receive the app instance and pull what they need off it, which keeps
them trivially testable — a test builds its own ``GhiaApp`` pointed at
``tmp_path`` and never touches the real ``~/.config`` tree.

The ``create_app`` factory is async because convention discovery (and,
later, default-branch detection) may do I/O.  We keep it lean for now;
tools that need external clients (GitHub, Docker) will acquire them
lazily off ``app`` when those clusters land.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ghia.config import Config, load_config
from ghia.redaction import install_filter, set_token
from ghia.session import SessionStore

logger = logging.getLogger(__name__)

__all__ = ["GhiaApp", "create_app"]


@dataclass
class GhiaApp:
    """Runtime context shared across MCP tool handlers.

    All fields are populated by :func:`create_app`; the dataclass is
    intentionally simple so tests can construct one directly when a
    full config isn't required.
    """

    config: Config
    session: SessionStore
    repo_root: Path
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("ghia")
    )
    # Background polling handle.  Owned by ghia.polling — start_polling
    # writes here, stop_polling clears it.  Declared on the dataclass
    # so its lifetime is visible at the type level rather than a
    # surprise attribute set at runtime.
    _polling_task: Optional[asyncio.Task] = None


def _default_session_path(repo_root: Path) -> Path:
    """Where session.json lives relative to the repo root."""

    return repo_root / "state" / "session.json"


async def create_app(
    repo_root: Path,
    config_path: Optional[Path] = None,
) -> GhiaApp:
    """Build a :class:`GhiaApp` with redaction wired up.

    Args:
        repo_root: Absolute path of the repo we're operating on.
            Used as the root for the session file and, later, as the
            path-traversal anchor for filesystem tools.
        config_path: Optional override for the config file location.
            ``None`` uses :func:`ghia.config.default_config_path`.

    Returns:
        A ready-to-use :class:`GhiaApp`.  The redaction filter is
        installed on the root logger and the config's token is
        registered before this function returns, so any logging that
        happens downstream is already token-safe.

    Raises:
        ConfigMissingError: propagated from :func:`load_config`.
    """

    repo_root = Path(repo_root).resolve()
    cfg = load_config(path=config_path)

    # Redaction is load-bearing for token safety: install the filter
    # on the ROOT logger (so it covers every sub-logger in the
    # process) and re-register the token even though load_config()
    # already did — idempotent, and guards against the token having
    # been cleared in a long-lived session.
    install_filter(logging.getLogger())
    set_token(cfg.token)

    session_path = _default_session_path(repo_root)
    session = SessionStore(session_path)

    return GhiaApp(
        config=cfg,
        session=session,
        repo_root=repo_root,
        logger=logging.getLogger("ghia"),
    )
