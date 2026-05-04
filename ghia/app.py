"""Composition root (TRD-011 INFRA, v0.2 refactor).

:class:`GhiaApp` bundles the dependencies a tool handler needs:
configuration, session store, logger, repo root, and the auto-detected
``owner/name`` slug.  Tool modules receive the app instance and pull
what they need off it, which keeps them trivially testable — a test
builds its own ``GhiaApp`` pointed at ``tmp_path`` and never touches
the real ``~/.config`` tree.

v0.2 changes:

* The repo is auto-detected from ``git remote get-url origin`` (see
  :mod:`ghia.repo_detect`) — no ``app.config.repo`` field anymore.
  ``app.repo_full_name`` is the new accessor.
* The config path is derived from the detected repo
  (``repos/<owner>__<name>.json``) — :func:`create_app` no longer
  takes a ``config_path`` override in production.  Tests use the
  override to point at a sandboxed file.
* No token registration: gh CLI owns the token.  The redaction
  filter is still installed defensively (token-shaped strings in
  unexpected log lines still get scrubbed by the regex safety net).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ghia.config import Config, config_path_for, load_config
from ghia.redaction import install_filter
from ghia.repo_detect import detect_repo
from ghia.session import SessionStore

logger = logging.getLogger(__name__)

__all__ = ["GhiaApp", "create_app"]


@dataclass
class GhiaApp:
    """Runtime context shared across MCP tool handlers.

    All fields are populated by :func:`create_app`; the dataclass is
    intentionally simple so tests can construct one directly when a
    full config isn't required.

    ``repo_full_name`` is the canonical ``"owner/name"`` string that
    every tool passes to ``gh_cli``.  It's stored as a single string
    (not a tuple) because every gh subcommand consumes that exact
    form via ``--repo`` and re-formatting per call would be silly.
    """

    config: Config
    session: SessionStore
    repo_root: Path
    repo_full_name: str
    # Where the per-repo Config lives on disk. Tools that need to
    # persist config edits (e.g. set_mode) write back to this path so
    # the choice survives stop/start. Optional because some tests
    # construct the dataclass directly without a backing file.
    config_path: Optional[Path] = None
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
    repo_root: Optional[Path] = None,
    *,
    config_path: Optional[Path] = None,
    repo_full_name: Optional[str] = None,
) -> GhiaApp:
    """Build a :class:`GhiaApp` with detection + config loading wired.

    Args:
        repo_root: Absolute path of the repo we're operating on.
            ``None`` (production default) uses ``Path.cwd()`` —
            Claude Code launches from the repo dir.
        config_path: Test-only override for the per-repo config file.
            When supplied, :func:`load_config` reads this exact path
            and the repo-detection step is skipped if
            ``repo_full_name`` is also supplied.
        repo_full_name: Test-only override that bypasses
            ``git remote get-url origin``.  Pass ``"owner/name"`` to
            avoid spawning git in unit tests.

    Returns:
        A ready-to-use :class:`GhiaApp`.

    Raises:
        ConfigMissingError: propagated from :func:`load_config`.
        RepoDetectionError: propagated from :func:`detect_repo` when
            no override was supplied and the cwd isn't a github repo.
    """

    root = Path(repo_root if repo_root is not None else Path.cwd()).resolve()

    # Detect repo first — no point loading a config we can't key off.
    # Tests supply ``repo_full_name`` to skip the git invocation.
    if repo_full_name is None:
        owner, name = detect_repo(root)
        full = f"{owner}/{name}"
    else:
        full = repo_full_name
        if "/" not in full:
            raise ValueError(
                f"repo_full_name must be 'owner/name', got {full!r}"
            )
        owner, name = full.split("/", 1)

    # Load the per-repo config (or the explicit path for tests).
    if config_path is not None:
        cfg = load_config(path=config_path)
        resolved_config_path: Optional[Path] = Path(config_path)
    else:
        cfg = load_config(owner=owner, name=name)
        resolved_config_path = config_path_for(owner, name)

    # Redaction stays installed defensively even though no token is
    # registered: the regex safety net still scrubs any token-shaped
    # substring that might appear in gh's stderr (paranoia is cheap).
    install_filter(logging.getLogger())

    session_path = _default_session_path(root)
    session = SessionStore(session_path)

    return GhiaApp(
        config=cfg,
        session=session,
        repo_root=root,
        repo_full_name=full,
        config_path=resolved_config_path,
        logger=logging.getLogger("ghia"),
    )
