"""Composition-root test (TRD-011 INFRA, v0.2 refactor).

Covers:
* ``create_app`` with a valid per-repo config returns a :class:`GhiaApp`.
* ``create_app`` surfaces ``ConfigMissingError`` for a missing file.
* The redaction filter is installed on the root logger during init.
* ``app.repo_full_name`` reflects the (overridden in test) detected slug.
* The session file path is anchored at ``<repo_root>/state/session.json``.
* Auto-detection via ``git remote get-url origin`` is bypassed via the
  ``repo_full_name`` keyword override (production code uses
  :mod:`ghia.repo_detect`).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from ghia.app import GhiaApp, create_app
from ghia.config import ConfigMissingError
from ghia.redaction import RedactionFilter


@pytest.fixture(autouse=True)
def _reset_filters() -> None:
    """Strip filters left by other tests."""

    root = logging.getLogger()
    before = list(root.filters)
    yield
    for f in list(root.filters):
        if f not in before:
            root.removeFilter(f)


def _write_valid_config(path: Path, **overrides: object) -> dict[str, object]:
    """Write a minimal but valid per-repo config JSON at ``path``."""

    payload: dict[str, object] = {
        "label": "ai-fix",
        "mode": "semi",
        "poll_interval_min": 30,
    }
    payload.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return payload


async def test_create_app_with_valid_config_returns_app(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cfg.json"
    _write_valid_config(cfg_path)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    app = await create_app(
        repo_root=repo_root,
        config_path=cfg_path,
        repo_full_name="octo/hello",
    )

    assert isinstance(app, GhiaApp)
    assert app.config.label == "ai-fix"
    assert app.repo_full_name == "octo/hello"
    assert app.repo_root == repo_root.resolve()
    assert isinstance(app.logger, logging.Logger)


async def test_create_app_installs_redaction_filter(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cfg.json"
    _write_valid_config(cfg_path)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    await create_app(
        repo_root=repo_root,
        config_path=cfg_path,
        repo_full_name="octo/hello",
    )

    root = logging.getLogger()
    assert any(isinstance(f, RedactionFilter) for f in root.filters), (
        f"expected a RedactionFilter on root logger; got {root.filters!r}"
    )


async def test_create_app_with_missing_config_raises(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with pytest.raises(ConfigMissingError):
        await create_app(
            repo_root=repo_root,
            config_path=tmp_path / "does_not_exist.json",
            repo_full_name="octo/hello",
        )


async def test_create_app_session_path_under_repo_root(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cfg.json"
    _write_valid_config(cfg_path)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    app = await create_app(
        repo_root=repo_root,
        config_path=cfg_path,
        repo_full_name="octo/hello",
    )

    # SessionStore's path is public; we expect it under repo_root/state/
    assert app.session.path == repo_root.resolve() / "state" / "session.json"


async def test_create_app_rejects_malformed_repo_full_name(tmp_path: Path) -> None:
    """``repo_full_name`` must contain a slash — single-token form is wrong."""

    cfg_path = tmp_path / "cfg.json"
    _write_valid_config(cfg_path)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with pytest.raises(ValueError):
        await create_app(
            repo_root=repo_root,
            config_path=cfg_path,
            repo_full_name="no-slash",
        )
