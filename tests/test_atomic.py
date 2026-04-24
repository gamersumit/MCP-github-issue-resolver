"""TRD-004-TEST — verify atomic file writer.

Covers:
* Successful text/binary write produces the expected content and no
  leftover ``.tmp`` files.
* A crash simulated between ``write`` and ``os.replace`` leaves the
  original file unchanged and removes the tempfile.
* Existing POSIX mode bits are preserved across writes.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest

from ghia import atomic


def _list_tmps(parent: Path, stem: str) -> list[Path]:
    return [p for p in parent.iterdir() if p.name.startswith(f"{stem}.tmp.")]


def test_text_write_happy_path(tmp_path: Path) -> None:
    target = tmp_path / "greeting.txt"
    atomic.atomic_write_text(target, "hello\n")

    assert target.read_text() == "hello\n"
    assert _list_tmps(tmp_path, "greeting.txt") == [], "tempfile leaked"


def test_bytes_write_happy_path(tmp_path: Path) -> None:
    target = tmp_path / "payload.bin"
    atomic.atomic_write_bytes(target, b"\x00\x01\x02")

    assert target.read_bytes() == b"\x00\x01\x02"
    assert _list_tmps(tmp_path, "payload.bin") == []


def test_overwrite_replaces_content(tmp_path: Path) -> None:
    target = tmp_path / "evolving.txt"
    atomic.atomic_write_text(target, "v1")
    atomic.atomic_write_text(target, "v2")
    assert target.read_text() == "v2"


def test_crash_between_write_and_rename_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a crash right before os.replace — original must survive."""
    target = tmp_path / "durable.txt"
    atomic.atomic_write_text(target, "original")

    boom_replace = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("simulated crash before rename")
    )
    monkeypatch.setattr(atomic.os, "replace", boom_replace)

    with pytest.raises(RuntimeError, match="simulated crash"):
        atomic.atomic_write_text(target, "new content")

    # Original content is intact.
    assert target.read_text() == "original"
    # No leftover tempfile.
    assert _list_tmps(tmp_path, "durable.txt") == [], "tempfile leaked on crash"


def test_crash_during_write_cleans_up_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the actual write blows up, no tempfile should linger."""
    target = tmp_path / "halffail.txt"

    def exploding_write_and_sync(*_a: Any, **_kw: Any) -> None:
        # Create the tmp file so we can confirm cleanup, then raise.
        _a[0].write_text("half")
        raise OSError("disk full, simulated")

    monkeypatch.setattr(atomic, "_write_and_sync", exploding_write_and_sync)

    with pytest.raises(OSError, match="disk full"):
        atomic.atomic_write_text(target, "anything")

    assert not target.exists()
    assert _list_tmps(tmp_path, "halffail.txt") == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only mode bit test")
def test_preserves_existing_mode(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    target.write_text("v1")
    os.chmod(target, 0o600)

    atomic.atomic_write_text(target, "v2")
    assert target.read_text() == "v2"

    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"mode not preserved: {oct(mode)}"


def test_creates_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deep" / "leaf.txt"
    atomic.atomic_write_text(target, "ok")
    assert target.read_text() == "ok"
