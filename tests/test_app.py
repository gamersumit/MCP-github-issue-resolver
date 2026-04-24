"""Composition-root test (TRD-011 INFRA).

Covers:
* ``create_app`` with a valid config file returns a :class:`GhiaApp`.
* The redaction filter is installed on the root logger during init
  (verify via ``logging.getLogger().filters``).
* ``create_app`` surfaces ``ConfigMissingError`` for a missing file
  so callers can handle the "wizard hasn't run yet" case.
* The session file path is anchored at ``<repo_root>/state/session.json``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from ghia import redaction
from ghia.app import GhiaApp, create_app
from ghia.config import ConfigMissingError
from ghia.redaction import RedactionFilter


@pytest.fixture(autouse=True)
def _reset_token_and_filters() -> None:
    """Clear redaction state and any filters left behind by other tests."""

    redaction.set_token(None)
    root = logging.getLogger()
    before = list(root.filters)
    yield
    # Remove any RedactionFilter instances this test added so we don't
    # leak state across the suite.
    for f in list(root.filters):
        if f not in before:
            root.removeFilter(f)
    redaction.set_token(None)


def _write_valid_config(path: Path, **overrides: object) -> dict[str, object]:
    """Write a minimal but valid config JSON at ``path``.  Returns its dict."""

    payload = {
        "token": "ghp_" + "a" * 36,
        "repo": "octo/hello",
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

    app = await create_app(repo_root=repo_root, config_path=cfg_path)

    assert isinstance(app, GhiaApp)
    assert app.config.repo == "octo/hello"
    assert app.repo_root == repo_root.resolve()
    assert isinstance(app.logger, logging.Logger)


async def test_create_app_installs_redaction_filter(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cfg.json"
    _write_valid_config(cfg_path)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    await create_app(repo_root=repo_root, config_path=cfg_path)

    root = logging.getLogger()
    assert any(isinstance(f, RedactionFilter) for f in root.filters), (
        f"expected a RedactionFilter on root logger; got {root.filters!r}"
    )


async def test_create_app_registers_token_for_redaction(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cfg.json"
    token = "ghp_" + "b" * 36
    _write_valid_config(cfg_path, token=token)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    await create_app(repo_root=repo_root, config_path=cfg_path)
    assert redaction.get_token() == token


async def test_create_app_with_missing_config_raises(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with pytest.raises(ConfigMissingError):
        await create_app(
            repo_root=repo_root,
            config_path=tmp_path / "does_not_exist.json",
        )


async def test_create_app_session_path_under_repo_root(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cfg.json"
    _write_valid_config(cfg_path)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    app = await create_app(repo_root=repo_root, config_path=cfg_path)

    # SessionStore's path is public; we expect it under repo_root/state/
    assert app.session.path == repo_root.resolve() / "state" / "session.json"


async def test_create_app_uses_default_config_path_when_home_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the HOME-as-config-root pattern works end-to-end."""

    # Point HOME at tmp_path so default_config_path resolves here.
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() honors HOME on POSIX; this makes the default resolve
    # to ``<tmp_path>/.config/github-issue-agent/config.json``.
    default_cfg = tmp_path / ".config" / "github-issue-agent" / "config.json"
    _write_valid_config(default_cfg)

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    app = await create_app(repo_root=repo_root)
    assert app.config.repo == "octo/hello"
