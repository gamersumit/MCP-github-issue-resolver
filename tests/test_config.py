"""Per-repo config loader tests (v0.2 refactor).

Covers:
* chmod 600 on persist (mode == 0o600) — kept defensive even though
  there's nothing sensitive in the file anymore
* malformed JSON rejected with ConfigMissingError
* schema violations (poll_interval below floor, extra fields) rejected
* missing file raises ConfigMissingError
* default_config_dir points at ~/.config/github-issue-agent/repos/
* config_path_for builds ``<owner>__<name>.json`` filenames
* round-trip via owner/name path resolves to the right file
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ghia.config import (
    Config,
    ConfigMissingError,
    config_path_for,
    default_config_dir,
    load_config,
    save_config,
)


def _sample_config(**overrides) -> Config:
    defaults = {
        "label": "ai-fix",
        "mode": "semi",
        "poll_interval_min": 30,
    }
    defaults.update(overrides)
    return Config(**defaults)


def test_default_config_dir_under_home() -> None:
    p = default_config_dir()
    assert p.name == "repos"
    assert p.parent.name == "github-issue-agent"
    assert p.parent.parent.name == ".config"
    # Anchored at the current user's home.
    assert str(p).startswith(str(Path.home()))


def test_config_path_for_uses_double_underscore_separator() -> None:
    """The owner/name pair becomes ``owner__name.json`` on disk."""

    p = config_path_for("octo", "hello")
    assert p.name == "octo__hello.json"


def test_poll_interval_floor_enforced() -> None:
    with pytest.raises(ValueError):
        Config(poll_interval_min=1)


def test_extra_fields_rejected() -> None:
    """Stale token/repo fields from v0.1 must surface as schema errors."""

    with pytest.raises(ValueError):
        Config(token="ghp_x")  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        Config(repo="o/r")  # type: ignore[call-arg]


@pytest.mark.skipif(os.name != "posix", reason="chmod semantics POSIX-only")
def test_save_config_is_chmod_600(tmp_path: Path) -> None:
    target = tmp_path / "octo__hello.json"
    save_config(_sample_config(), path=target)

    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "alice__widgets.json"
    cfg = _sample_config(mode="full", label="bug")
    save_config(cfg, path=target)

    loaded = load_config(path=target)
    assert loaded.label == "bug"
    assert loaded.mode == "full"


def test_load_via_owner_name_uses_default_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``load_config(owner=, name=)`` must consult ``default_config_dir``."""

    # Redirect the default dir into tmp_path so the test doesn't touch
    # the user's real config tree.
    target_dir = tmp_path / "repos"
    monkeypatch.setattr(
        "ghia.config.default_config_dir", lambda: target_dir
    )

    save_config(_sample_config(label="custom"), owner="o", name="r")

    loaded = load_config(owner="o", name="r")
    assert loaded.label == "custom"
    assert (target_dir / "o__r.json").exists()


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
    bad.write_text('{"poll_interval_min": 1}')

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
    path.write_text("{}")

    cfg = load_config(path=path)
    assert cfg.mode == "semi"
    assert cfg.label == "ai-fix"
    assert cfg.poll_interval_min == 30


def test_load_or_save_without_path_or_owner_raises(tmp_path: Path) -> None:
    """Missing both ``path=`` and ``owner=,name=`` must surface clearly."""

    with pytest.raises(TypeError):
        load_config()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        save_config(_sample_config())  # type: ignore[call-arg]
