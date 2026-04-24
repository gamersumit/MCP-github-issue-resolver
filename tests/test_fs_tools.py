"""Filesystem tool tests (TRD-022-TEST).

Covers each fs tool for:
* happy-path response shape
* path-traversal guard rejections (.., absolute escape, escaping symlink)
* missing-file errors
* atomic write smoke check (no .tmp leftovers)
* read_file size cap with INVALID_INPUT
* search_codebase skips .git/, node_modules/, __pycache__/
* read_multiple_files reports per-file errors without whole-call failure
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from ghia.app import GhiaApp
from ghia.config import Config
from ghia.errors import ErrorCode
from ghia.session import SessionStore
from ghia.tools import fs


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def app(repo_root: Path, tmp_path: Path) -> GhiaApp:
    """A minimal GhiaApp pointed at ``repo_root``.

    fs tools only use ``app.repo_root``; we don't need redaction, real
    config, or the convention scanner — so we hand-build the dataclass
    rather than going through ``create_app`` (which requires a config
    file on disk).
    """

    cfg = Config(label="ai-fix", mode="semi", poll_interval_min=30)
    session_path = tmp_path / "session.json"
    session = SessionStore(session_path)
    return GhiaApp(config=cfg, session=session, repo_root=repo_root, repo_full_name="octo/hello", logger=logging.getLogger("ghia-test"),
    )


# ----------------------------------------------------------------------
# read_file
# ----------------------------------------------------------------------


async def test_read_file_happy_path(app: GhiaApp, repo_root: Path) -> None:
    target = repo_root / "hello.txt"
    target.write_text("hi there\n", encoding="utf-8")

    resp = await fs.read_file(app, "hello.txt")

    assert resp.success, resp.error
    assert resp.data["content"] == "hi there\n"
    assert resp.data["encoding"] == "utf-8"
    assert resp.data["size"] == len("hi there\n".encode("utf-8"))
    assert resp.data["path"] == "hello.txt"


async def test_read_file_traversal_dotdot_rejected(app: GhiaApp) -> None:
    resp = await fs.read_file(app, "../escape.txt")
    assert not resp.success
    assert resp.code == ErrorCode.PATH_TRAVERSAL


async def test_read_file_traversal_absolute_escape_rejected(
    app: GhiaApp, tmp_path: Path
) -> None:
    outside = tmp_path / "sibling" / "x.txt"
    outside.parent.mkdir()
    outside.write_text("not yours")
    resp = await fs.read_file(app, str(outside))
    assert not resp.success
    assert resp.code == ErrorCode.PATH_TRAVERSAL


@pytest.mark.skipif(os.name != "posix", reason="symlinks: POSIX-only")
async def test_read_file_symlink_escape_rejected(
    app: GhiaApp, repo_root: Path, tmp_path: Path
) -> None:
    target = tmp_path / "outside.txt"
    target.write_text("secrets")
    link = repo_root / "evil"
    link.symlink_to(target)

    resp = await fs.read_file(app, "evil")
    assert not resp.success
    assert resp.code == ErrorCode.PATH_TRAVERSAL


async def test_read_file_missing_returns_file_not_found(app: GhiaApp) -> None:
    resp = await fs.read_file(app, "no-such-file.txt")
    assert not resp.success
    assert resp.code == ErrorCode.FILE_NOT_FOUND


async def test_read_file_too_large_returns_invalid_input(
    app: GhiaApp, repo_root: Path
) -> None:
    target = repo_root / "big.txt"
    target.write_text("x" * 100)
    resp = await fs.read_file(app, "big.txt", max_bytes=10)
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT
    assert "too large" in (resp.error or "").lower()


async def test_read_file_non_utf8_uses_replace_with_warning(
    app: GhiaApp, repo_root: Path
) -> None:
    target = repo_root / "weird.bin"
    # 0xFF is invalid in utf-8.
    target.write_bytes(b"hello \xff world")

    resp = await fs.read_file(app, "weird.bin")
    assert resp.success
    assert "warning" in resp.data
    # Replacement character indicates the decode happened with errors="replace".
    assert "�" in resp.data["content"]


async def test_read_file_directory_path_returns_invalid_input(
    app: GhiaApp, repo_root: Path
) -> None:
    (repo_root / "subdir").mkdir()
    resp = await fs.read_file(app, "subdir")
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT


# ----------------------------------------------------------------------
# write_file
# ----------------------------------------------------------------------


async def test_write_file_happy_path(app: GhiaApp, repo_root: Path) -> None:
    resp = await fs.write_file(app, "out.txt", "payload")
    assert resp.success, resp.error
    assert resp.data["bytes_written"] == len(b"payload")
    assert (repo_root / "out.txt").read_text(encoding="utf-8") == "payload"


async def test_write_file_creates_parent_dirs(app: GhiaApp, repo_root: Path) -> None:
    resp = await fs.write_file(app, "deep/nested/dir/out.txt", "ok")
    assert resp.success
    assert (repo_root / "deep" / "nested" / "dir" / "out.txt").read_text() == "ok"


async def test_write_file_traversal_rejected(app: GhiaApp) -> None:
    resp = await fs.write_file(app, "../oops.txt", "no")
    assert not resp.success
    assert resp.code == ErrorCode.PATH_TRAVERSAL


async def test_write_file_no_tmp_leftovers(app: GhiaApp, repo_root: Path) -> None:
    """Atomic write must not leave any ``.tmp.*`` artifacts behind."""

    resp = await fs.write_file(app, "ok.txt", "done")
    assert resp.success

    leftovers = [p.name for p in repo_root.iterdir() if ".tmp." in p.name]
    assert leftovers == [], f"unexpected tmp files: {leftovers}"


async def test_write_file_overwrites_existing(app: GhiaApp, repo_root: Path) -> None:
    target = repo_root / "x.txt"
    target.write_text("v1")
    resp = await fs.write_file(app, "x.txt", "v2")
    assert resp.success
    assert target.read_text() == "v2"


async def test_write_file_refuses_to_overwrite_directory(
    app: GhiaApp, repo_root: Path
) -> None:
    (repo_root / "adir").mkdir()
    resp = await fs.write_file(app, "adir", "no")
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT


async def test_write_file_non_string_content_rejected(app: GhiaApp) -> None:
    resp = await fs.write_file(app, "f.txt", 123)  # type: ignore[arg-type]
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT


# ----------------------------------------------------------------------
# list_directory
# ----------------------------------------------------------------------


async def test_list_directory_happy_path(app: GhiaApp, repo_root: Path) -> None:
    (repo_root / "a.txt").write_text("a")
    (repo_root / "b.txt").write_text("bb")
    (repo_root / "sub").mkdir()

    resp = await fs.list_directory(app, ".")
    assert resp.success
    names = {e["name"] for e in resp.data["entries"]}
    assert {"a.txt", "b.txt", "sub"}.issubset(names)
    types = {e["name"]: e["type"] for e in resp.data["entries"]}
    assert types["a.txt"] == "file"
    assert types["sub"] == "dir"


async def test_list_directory_hides_dotfiles_by_default(
    app: GhiaApp, repo_root: Path
) -> None:
    (repo_root / ".hidden").write_text("h")
    (repo_root / "visible").write_text("v")

    resp = await fs.list_directory(app, ".")
    names = {e["name"] for e in resp.data["entries"]}
    assert "visible" in names
    assert ".hidden" not in names


async def test_list_directory_include_hidden(
    app: GhiaApp, repo_root: Path
) -> None:
    (repo_root / ".hidden").write_text("h")

    resp = await fs.list_directory(app, ".", include_hidden=True)
    names = {e["name"] for e in resp.data["entries"]}
    assert ".hidden" in names


async def test_list_directory_traversal_rejected(app: GhiaApp) -> None:
    resp = await fs.list_directory(app, "../")
    assert not resp.success
    assert resp.code == ErrorCode.PATH_TRAVERSAL


async def test_list_directory_missing_returns_file_not_found(app: GhiaApp) -> None:
    resp = await fs.list_directory(app, "nope")
    assert not resp.success
    assert resp.code == ErrorCode.FILE_NOT_FOUND


async def test_list_directory_path_is_file_returns_invalid_input(
    app: GhiaApp, repo_root: Path
) -> None:
    (repo_root / "file.txt").write_text("x")
    resp = await fs.list_directory(app, "file.txt")
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT


@pytest.mark.skipif(os.name != "posix", reason="symlinks: POSIX-only")
async def test_list_directory_classifies_symlinks(
    app: GhiaApp, repo_root: Path
) -> None:
    target = repo_root / "real.txt"
    target.write_text("ok")
    (repo_root / "link.txt").symlink_to(target)

    resp = await fs.list_directory(app, ".")
    types = {e["name"]: e["type"] for e in resp.data["entries"]}
    assert types["link.txt"] == "symlink"


# ----------------------------------------------------------------------
# search_codebase
# ----------------------------------------------------------------------


async def test_search_codebase_finds_substring(
    app: GhiaApp, repo_root: Path
) -> None:
    (repo_root / "a.py").write_text("def foo():\n    return 'hello'\n")
    (repo_root / "b.py").write_text("hello world\n")

    resp = await fs.search_codebase(app, "hello")
    assert resp.success
    assert resp.data["total_matches"] == 2
    paths = {m["path"] for m in resp.data["matches"]}
    assert paths == {"a.py", "b.py"}
    assert resp.data["truncated"] is False


async def test_search_codebase_skips_dotgit_and_node_modules(
    app: GhiaApp, repo_root: Path
) -> None:
    (repo_root / ".git").mkdir()
    (repo_root / ".git" / "HEAD").write_text("hello inside .git")
    (repo_root / "node_modules").mkdir()
    (repo_root / "node_modules" / "x.js").write_text("hello in node_modules")
    (repo_root / "__pycache__").mkdir()
    (repo_root / "__pycache__" / "cached.pyc").write_text("hello in pycache")
    (repo_root / "src.py").write_text("hello in source")

    resp = await fs.search_codebase(app, "hello")
    assert resp.success
    paths = {m["path"] for m in resp.data["matches"]}
    assert paths == {"src.py"}


async def test_search_codebase_truncates_at_max_matches(
    app: GhiaApp, repo_root: Path
) -> None:
    (repo_root / "many.txt").write_text("\n".join(["hello"] * 50))

    resp = await fs.search_codebase(app, "hello", max_matches=5)
    assert resp.success
    assert resp.data["total_matches"] == 50
    assert len(resp.data["matches"]) == 5
    assert resp.data["truncated"] is True


async def test_search_codebase_skips_files_over_1mb(
    app: GhiaApp, repo_root: Path
) -> None:
    big = repo_root / "big.txt"
    # > 1 MB heuristic; pad to 1.5 MB.
    big.write_text("hello\n" + "x" * (1_500_000))

    resp = await fs.search_codebase(app, "hello")
    assert resp.success
    # The big file should have been skipped.
    paths = {m["path"] for m in resp.data["matches"]}
    assert "big.txt" not in paths


async def test_search_codebase_empty_query_rejected(app: GhiaApp) -> None:
    resp = await fs.search_codebase(app, "")
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT


async def test_search_codebase_match_includes_line_text_and_number(
    app: GhiaApp, repo_root: Path
) -> None:
    (repo_root / "f.txt").write_text("first\nsecond hello\nthird\n")
    resp = await fs.search_codebase(app, "hello")
    assert resp.success
    [match] = resp.data["matches"]
    assert match["line"] == 2
    assert match["text"] == "second hello"


# ----------------------------------------------------------------------
# get_repo_structure
# ----------------------------------------------------------------------


async def test_get_repo_structure_lists_paths(
    app: GhiaApp, repo_root: Path
) -> None:
    (repo_root / "a.txt").write_text("a")
    (repo_root / "sub").mkdir()
    (repo_root / "sub" / "b.txt").write_text("b")

    resp = await fs.get_repo_structure(app, max_depth=3)
    assert resp.success
    tree = resp.data["tree"]
    assert "a.txt" in tree
    assert "sub/b.txt" in tree


async def test_get_repo_structure_respects_max_depth(
    app: GhiaApp, repo_root: Path
) -> None:
    (repo_root / "l1").mkdir()
    (repo_root / "l1" / "l2").mkdir()
    (repo_root / "l1" / "l2" / "deep.txt").write_text("d")
    (repo_root / "l1" / "shallow.txt").write_text("s")

    resp = await fs.get_repo_structure(app, max_depth=1)
    assert resp.success
    tree = resp.data["tree"]
    # depth=1: only top-level entries, no l1/shallow.txt or deeper.
    assert "l1/shallow.txt" not in tree
    assert "l1/l2/deep.txt" not in tree


async def test_get_repo_structure_skips_skip_dirs(
    app: GhiaApp, repo_root: Path
) -> None:
    (repo_root / ".git").mkdir()
    (repo_root / ".git" / "HEAD").write_text("ref")
    (repo_root / "node_modules").mkdir()
    (repo_root / "node_modules" / "x.js").write_text("x")
    (repo_root / "src.py").write_text("ok")

    resp = await fs.get_repo_structure(app, max_depth=5)
    tree = resp.data["tree"]
    assert "src.py" in tree
    assert not any(t.startswith(".git") for t in tree)
    assert not any(t.startswith("node_modules") for t in tree)


async def test_get_repo_structure_invalid_depth_rejected(app: GhiaApp) -> None:
    resp = await fs.get_repo_structure(app, max_depth=0)
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT


# ----------------------------------------------------------------------
# read_multiple_files
# ----------------------------------------------------------------------


async def test_read_multiple_files_reports_per_file_errors(
    app: GhiaApp, repo_root: Path
) -> None:
    (repo_root / "ok.txt").write_text("here")

    resp = await fs.read_multiple_files(
        app, ["ok.txt", "missing.txt", "../escape"]
    )
    assert resp.success  # whole call doesn't fail
    by_path = {f["path"]: f for f in resp.data["files"]}
    assert by_path["ok.txt"]["content"] == "here"
    assert "error" in by_path["missing.txt"]
    # The traversal entry uses the raw path the caller supplied as its
    # ``path`` field so callers can correlate.
    assert "error" in by_path["../escape"]
    assert "path_traversal" in by_path["../escape"]["error"]


async def test_read_multiple_files_empty_list(app: GhiaApp) -> None:
    resp = await fs.read_multiple_files(app, [])
    assert resp.success
    assert resp.data["files"] == []


async def test_read_multiple_files_non_list_rejected(app: GhiaApp) -> None:
    resp = await fs.read_multiple_files(app, "not-a-list")  # type: ignore[arg-type]
    assert not resp.success
    assert resp.code == ErrorCode.INVALID_INPUT


async def test_read_multiple_files_non_string_entry(
    app: GhiaApp, repo_root: Path
) -> None:
    (repo_root / "a.txt").write_text("a")
    resp = await fs.read_multiple_files(app, ["a.txt", 123])  # type: ignore[list-item]
    assert resp.success
    by_path = {f["path"]: f for f in resp.data["files"]}
    assert "a.txt" in by_path
    assert "error" in by_path["123"]
