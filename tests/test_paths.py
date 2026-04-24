"""TRD-005-TEST — verify path-traversal guard utility.

Covers AC-014-1, AC-014-2, AC-014-3:
* ``..`` traversal rejected
* absolute escape rejected
* symlink-escape rejected
* legitimate relative paths resolved successfully
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ghia.paths import PathTraversalError, resolve_inside


def test_relative_path_resolves(repo_root: Path) -> None:
    resolved = resolve_inside(repo_root, "src/main.py")
    assert resolved == (repo_root / "src" / "main.py").resolve()


def test_nested_relative_path_resolves(repo_root: Path) -> None:
    resolved = resolve_inside(repo_root, "a/b/c/d.txt")
    assert resolved.is_relative_to(repo_root.resolve())


def test_dotdot_escape_rejected(repo_root: Path) -> None:
    with pytest.raises(PathTraversalError) as info:
        resolve_inside(repo_root, "../outside.txt")
    assert info.value.attempted == "../outside.txt"


def test_deep_dotdot_escape_rejected(repo_root: Path) -> None:
    with pytest.raises(PathTraversalError):
        resolve_inside(repo_root, "a/b/../../../escape.txt")


def test_absolute_escape_rejected(repo_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "sibling" / "file.txt"
    with pytest.raises(PathTraversalError):
        resolve_inside(repo_root, str(outside))


def test_absolute_inside_repo_accepted(repo_root: Path) -> None:
    """An absolute path that happens to live inside repo_root is fine."""
    inside = repo_root / "ok.txt"
    resolved = resolve_inside(repo_root, str(inside))
    assert resolved == inside.resolve()


@pytest.mark.skipif(os.name != "posix", reason="symlinks: POSIX-only in CI")
def test_symlink_escape_rejected(repo_root: Path, tmp_path: Path) -> None:
    target = tmp_path / "outside-file.txt"
    target.write_text("shh")

    link = repo_root / "evil_link.txt"
    link.symlink_to(target)

    with pytest.raises(PathTraversalError):
        resolve_inside(repo_root, "evil_link.txt")


@pytest.mark.skipif(os.name != "posix", reason="symlinks: POSIX-only in CI")
def test_symlink_inside_repo_accepted(repo_root: Path) -> None:
    real = repo_root / "data" / "real.txt"
    real.parent.mkdir(parents=True)
    real.write_text("ok")

    link = repo_root / "link.txt"
    link.symlink_to(real)

    resolved = resolve_inside(repo_root, "link.txt")
    assert resolved == real.resolve()


def test_empty_candidate_rejected(repo_root: Path) -> None:
    with pytest.raises(PathTraversalError):
        resolve_inside(repo_root, "")


def test_attempted_attribute_populated(repo_root: Path) -> None:
    try:
        resolve_inside(repo_root, "../nope")
    except PathTraversalError as exc:
        assert exc.attempted == "../nope"
    else:  # pragma: no cover
        pytest.fail("expected PathTraversalError")
