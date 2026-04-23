"""TRD-003-TEST — verify config loader.

Covers:
* chmod 600 on persist (mode == 0o600)
* malformed JSON rejected with ConfigMissingError
* schema violations rejected with ConfigMissingError
* missing file raises ConfigMissingError
* token is registered with ghia.redaction on save + load
* default_config_path points at ~/.config/github-issue-agent/config.json
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ghia import redaction
from ghia.config import (
    Config,
    ConfigMissingError,
    default_config_path,
    load_config,
    save_config,
)


@pytest.fixture(autouse=True)
def _reset_token() -> None:
    redaction.set_token(None)
    yield
    redaction.set_token(None)


def _sample_config(**overrides) -> Config:
    defaults = {
        "token": "ghp_" + "x" * 36,
        "repo": "octo/hello",
        "label": "ai-fix",
        "mode": "semi",
        "poll_interval_min": 30,
    }
    defaults.update(overrides)
    return Config(**defaults)


def test_default_config_path_under_home() -> None:
    p = default_config_path()
    assert p.name == "config.json"
    assert p.parent.name == "github-issue-agent"
    assert p.parent.parent.name == ".config"
    # Anchored at the current user's home.
    assert str(p).startswith(str(Path.home()))


def test_repo_field_must_match_owner_slash_name() -> None:
    with pytest.raises(ValueError):
        Config(token="t" * 10, repo="not-a-repo")


def test_poll_interval_floor_enforced() -> None:
    with pytest.raises(ValueError):
        Config(token="t" * 10, repo="o/r", poll_interval_min=1)


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValueError):
        Config(token="t" * 10, repo="o/r", nonsense="x")  # type: ignore[call-arg]


@pytest.mark.skipif(os.name != "posix", reason="chmod semantics POSIX-only")
def test_save_config_is_chmod_600(tmp_path: Path) -> None:
    target = tmp_path / "cfg.json"
    save_config(_sample_config(), path=target)

    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "cfg.json"
    cfg = _sample_config(repo="alice/widgets", mode="full")
    save_config(cfg, path=target)

    loaded = load_config(path=target)
    assert loaded.repo == "alice/widgets"
    assert loaded.mode == "full"
    assert loaded.token == cfg.token


def test_save_registers_token_for_redaction(tmp_path: Path) -> None:
    target = tmp_path / "cfg.json"
    cfg = _sample_config(token="ghp_" + "y" * 36)
    save_config(cfg, path=target)

    assert redaction.get_token() == cfg.token


def test_load_registers_token_for_redaction(tmp_path: Path) -> None:
    target = tmp_path / "cfg.json"
    cfg = _sample_config(token="ghp_" + "z" * 36)
    save_config(cfg, path=target)

    redaction.set_token(None)  # clear between save and load
    load_config(path=target)
    assert redaction.get_token() == cfg.token


def test_load_missing_file_raises_config_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigMissingError):
        load_config(path=tmp_path / "nope.json")


def test_load_malformed_json_raises(tmp_path: Path) -> None:
    bad = tmp_path / "cfg.json"
    bad.write_text("{this is not json}")

    with pytest.raises(ConfigMissingError) as info:
        load_config(path=bad)
    # Cause preserved for diagnostics.
    assert info.value.__cause__ is not None


def test_load_schema_violation_raises(tmp_path: Path) -> None:
    bad = tmp_path / "cfg.json"
    bad.write_text('{"token": "t", "repo": "nope-no-slash"}')

    with pytest.raises(ConfigMissingError):
        load_config(path=bad)


def test_load_non_object_json_raises(tmp_path: Path) -> None:
    bad = tmp_path / "cfg.json"
    bad.write_text('["array", "not", "object"]')

    with pytest.raises(ConfigMissingError):
        load_config(path=bad)


def test_default_values_filled_in(tmp_path: Path) -> None:
    """Mode, label, poll_interval should have defaults even if absent."""
    path = tmp_path / "cfg.json"
    path.write_text(
        '{"token": "ghp_' + "a" * 36 + '", "repo": "o/r"}'
    )

    cfg = load_config(path=path)
    assert cfg.mode == "semi"
    assert cfg.label == "ai-fix"
    assert cfg.poll_interval_min == 30
