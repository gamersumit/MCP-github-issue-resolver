"""Configuration loader / writer (TRD-003).

Config lives at ``~/.config/github-issue-agent/config.json`` by default.
Writes are atomic (via :mod:`ghia.atomic`) and the file is chmod-600
immediately after the rename, so the token on disk is only readable by
the owning user.  The token is also registered with
:mod:`ghia.redaction` on every load/save so it's filtered out of logs.

Satisfies REQ-023 (token security), REQ-002 (setup persistence).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ghia.atomic import atomic_write_text
from ghia.redaction import set_token

logger = logging.getLogger(__name__)

__all__ = [
    "Config",
    "ConfigMissingError",
    "default_config_path",
    "load_config",
    "save_config",
]


_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$")


class ConfigMissingError(Exception):
    """Raised when the config file cannot be loaded.

    Wraps three distinct failure modes — file-not-found, invalid JSON,
    and schema validation failure — so callers can treat "no usable
    config" uniformly.  The underlying cause is preserved on
    ``__cause__`` for diagnostic logging.
    """


class Config(BaseModel):
    """User / machine configuration for the issue agent.

    All fields are validated on load and on save.  Unknown fields are
    rejected so stale config files surface early rather than silently
    ignoring options.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    token: str = Field(..., min_length=1, description="GitHub PAT")
    repo: str = Field(..., description="Target repo in owner/name form")
    label: str = Field(default="ai-fix", min_length=1)
    mode: Literal["semi", "full"] = "semi"
    poll_interval_min: int = Field(default=30, ge=5)
    test_command: Optional[str] = None
    lint_command: Optional[str] = None

    @field_validator("repo")
    @classmethod
    def _validate_repo(cls, value: str) -> str:
        if not _REPO_RE.match(value):
            raise ValueError(
                f"repo must match 'owner/name' (got {value!r})"
            )
        return value

    @field_validator("token")
    @classmethod
    def _strip_token(cls, value: str) -> str:
        # Accept whitespace-padded tokens (copy-paste from terminal)
        # but store the canonical trimmed form.
        return value.strip()


def default_config_path() -> Path:
    """Return ``~/.config/github-issue-agent/config.json``."""

    return Path.home() / ".config" / "github-issue-agent" / "config.json"


def load_config(path: Optional[Path] = None) -> Config:
    """Load + validate a config file.

    Args:
        path: Override; defaults to :func:`default_config_path`.

    Returns:
        Parsed :class:`Config`.  Also registers the token with
        :mod:`ghia.redaction` so subsequent log records scrub it.

    Raises:
        ConfigMissingError: if the file is missing, unparsable, or
            fails Pydantic validation.  The cause (``FileNotFoundError``,
            ``json.JSONDecodeError``, or ``ValidationError``) is kept on
            ``__cause__``.
    """

    target = path if path is not None else default_config_path()
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
        cfg = Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigMissingError(
            f"config file at {target} failed validation: {exc.error_count()} errors"
        ) from exc

    # Register the token for redaction so nothing logs it verbatim.
    set_token(cfg.token)
    return cfg


def save_config(cfg: Config, path: Optional[Path] = None) -> None:
    """Persist ``cfg`` to ``path`` with chmod 600.

    Flow: atomic write → chmod → register token for redaction.

    Args:
        cfg: Validated :class:`Config`.
        path: Override; defaults to :func:`default_config_path`.
    """

    target = path if path is not None else default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = cfg.model_dump_json(indent=2)
    atomic_write_text(target, payload + "\n")

    # chmod AFTER the rename — the tempfile was 0666 & ~umask.  On
    # non-POSIX os.chmod still exists but has limited effect; we call
    # it unconditionally so tests that check the bit can pass on Linux.
    try:
        os.chmod(target, 0o600)
    except OSError as exc:
        logger.warning("could not chmod 600 on %s: %s", target, exc)

    # Register the (possibly new) token with the redaction filter.
    set_token(cfg.token)
