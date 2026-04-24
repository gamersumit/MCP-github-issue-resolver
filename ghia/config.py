"""Per-repo configuration loader / writer (v0.2 refactor).

v0.1 stored a single global ``~/.config/github-issue-agent/config.json``
keyed nowhere (one user → one repo → one token).  v0.2 ditches the
global file entirely:

* **No token field.**  Auth is delegated to ``gh`` CLI (see
  :mod:`ghia.integrations.gh_cli`); the keychain holds the token,
  not us.
* **No repo field.**  The repo is implicit in the file name —
  ``~/.config/github-issue-agent/repos/<owner>__<name>.json`` — so
  one user can have N repos with N different active gh accounts and
  the configs never collide.

Files are still chmod 600 even though there's nothing sensitive in
them — defense in depth, and it future-proofs the format if we ever
re-add a field that needs protection (e.g. a per-repo token override).

Satisfies REQ-002 (setup persistence).  The token-safety part of
REQ-023 moves to the gh-CLI integration; the config layer no longer
holds anything to leak.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ghia.atomic import atomic_write_text
from ghia.repo_detect import config_filename_for

logger = logging.getLogger(__name__)

__all__ = [
    "Config",
    "ConfigMissingError",
    "default_config_dir",
    "config_path_for",
    "load_config",
    "save_config",
]


class ConfigMissingError(Exception):
    """Raised when the per-repo config file cannot be loaded.

    Wraps three failure modes — file-not-found, invalid JSON, schema
    validation — so callers (typically :mod:`server`) can treat "no
    usable config" uniformly and prompt the user to re-run the wizard.
    """


class Config(BaseModel):
    """Per-repo configuration for the issue agent.

    Note: ``repo`` is intentionally NOT a field — it's encoded in the
    filename so a stale config file can never disagree with its name.
    Same logic for ``token``: gh owns it.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    label: str = Field(default="ai-fix", min_length=1)
    mode: Literal["semi", "full"] = "semi"
    poll_interval_min: int = Field(default=30, ge=5)
    test_command: Optional[str] = None
    lint_command: Optional[str] = None


def default_config_dir() -> Path:
    """Return ``~/.config/github-issue-agent/repos/``.

    Pulled out so tests can monkeypatch this single function rather
    than chasing the path through every caller.  The ``repos/``
    subdir keeps the per-repo files separated from any future
    machine-wide settings we might re-add at the parent level.
    """

    return Path.home() / ".config" / "github-issue-agent" / "repos"


def config_path_for(owner: str, name: str) -> Path:
    """Return the per-repo config path for ``<owner>/<name>``.

    Uses :func:`ghia.repo_detect.config_filename_for` so the
    ``__``-separator convention lives in exactly one place.
    """

    return default_config_dir() / config_filename_for(owner, name)


def load_config(
    *,
    owner: Optional[str] = None,
    name: Optional[str] = None,
    path: Optional[Path] = None,
) -> Config:
    """Load + validate a per-repo config file.

    Two ways to call:
    * ``load_config(owner="o", name="n")`` — derives the path via
      :func:`config_path_for`.  This is the production caller.
    * ``load_config(path=...)`` — explicit override for tests that
      want a sandboxed file location.

    Raises:
        ConfigMissingError: if the file is missing, unparsable, or
            fails Pydantic validation.  Original exception preserved
            on ``__cause__``.
    """

    target = _resolve_target(owner=owner, name=name, path=path)

    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigMissingError(
            f"config file not found at {target}"
        ) from exc
    except OSError as exc:
        raise ConfigMissingError(
            f"could not read config at {target}: {exc}"
        ) from exc

    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigMissingError(
            f"config file at {target} is not valid JSON: {exc.msg}"
        ) from exc

    if not isinstance(data, dict):
        raise ConfigMissingError(
            f"config file at {target} must be a JSON object"
        )

    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigMissingError(
            f"config file at {target} failed validation: {exc.error_count()} errors"
        ) from exc


def save_config(
    cfg: Config,
    *,
    owner: Optional[str] = None,
    name: Optional[str] = None,
    path: Optional[Path] = None,
) -> None:
    """Persist ``cfg`` with chmod 600.

    Same two-way calling convention as :func:`load_config`.

    chmod 600 is kept even though no field is sensitive — partly
    defensive, partly so a future re-introduction of a token-class
    field doesn't quietly relax the permission bit.  Tests still
    assert the bit, so a regression here would surface immediately.
    """

    target = _resolve_target(owner=owner, name=name, path=path)
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = cfg.model_dump_json(indent=2)
    atomic_write_text(target, payload + "\n")

    # chmod AFTER the rename — the tempfile was created with 0666 &
    # ~umask.  Non-POSIX hosts get a best-effort chmod that may be a
    # no-op; we log the failure but don't escalate.
    try:
        os.chmod(target, 0o600)
    except OSError as exc:
        logger.warning("could not chmod 600 on %s: %s", target, exc)


def _resolve_target(
    *,
    owner: Optional[str],
    name: Optional[str],
    path: Optional[Path],
) -> Path:
    """Reconcile the two calling conventions into a single Path.

    Either ``path`` OR ``(owner, name)`` must be supplied.  Mixing
    them is allowed (path wins) but the test suite consistently uses
    one or the other to keep intent clear.
    """

    if path is not None:
        return path
    if owner is None or name is None:
        raise TypeError(
            "load_config/save_config requires either path= or both owner= and name="
        )
    return config_path_for(owner, name)
