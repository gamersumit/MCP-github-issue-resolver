"""Filesystem MCP tools (TRD-022).

Six user-visible tools for safe, sandboxed filesystem access:

* ``read_file`` — read a UTF-8 text file (with size cap and a
  best-effort decode for non-UTF-8 bytes)
* ``write_file`` — atomic write of UTF-8 text via :mod:`ghia.atomic`
* ``list_directory`` — shallow directory listing with file/dir/symlink
  classification
* ``search_codebase`` — plain-text substring search across the repo
  (skips VCS / virtualenv / build directories, skips large files)
* ``get_repo_structure`` — depth-limited path tree
* ``read_multiple_files`` — batch read with per-file error reporting

**Path-traversal guard runs BEFORE any I/O on every tool.**  Every
path argument is routed through :func:`ghia.paths.resolve_inside`; a
:class:`PathTraversalError` becomes a structured ``PATH_TRAVERSAL``
error, never an exception.

Satisfies REQ-014, REQ-015.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

from ghia.app import GhiaApp
from ghia.atomic import atomic_write_text
from ghia.errors import ErrorCode, ToolResponse, err, ok, wrap_tool
from ghia.paths import PathTraversalError, resolve_inside

logger = logging.getLogger(__name__)

__all__ = [
    "read_file",
    "write_file",
    "list_directory",
    "search_codebase",
    "get_repo_structure",
    "read_multiple_files",
]


# Directories we always skip when walking the repo (VCS metadata,
# package caches, build outputs, virtualenvs).  Matched by *name*, not
# by full path, so they're skipped at any depth.  Centralized here so
# search_codebase and get_repo_structure agree.
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
})

# Files larger than this in search_codebase are skipped: probably
# binary, generated, or vendored, and grepping them is wasteful and
# noisy.  read_file has its own (caller-overridable) cap.
_SEARCH_MAX_FILE_BYTES = 1_000_000


# ----------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------


def _classify(p: Path) -> str:
    """Classify a path entry as ``"symlink" | "dir" | "file"``.

    Symlink takes priority over dir/file because callers care about
    "this entry might cross a boundary" more than the link's target
    type.  Falls back to ``"file"`` for sockets, fifos, etc. — the
    listing is best-effort, not a full POSIX type taxonomy.
    """

    if p.is_symlink():
        return "symlink"
    if p.is_dir():
        return "dir"
    return "file"


def _safe_size(p: Path) -> int:
    """Return file size in bytes, or 0 if stat fails (broken symlink etc.)."""

    try:
        return p.stat().st_size
    except OSError:
        return 0


def _path_traversal_err(exc: PathTraversalError) -> ToolResponse:
    """Uniform wrapping for guard failures.  Hides full paths from output."""

    return err(ErrorCode.PATH_TRAVERSAL, str(exc))


def _rel(repo_root: Path, target: Path) -> str:
    """Return ``target`` as a forward-slash path relative to repo_root.

    We always emit forward slashes so output is stable across
    Windows/POSIX — callers (and tests) shouldn't have to branch on
    ``os.sep``.
    """

    try:
        rel = target.resolve().relative_to(repo_root.resolve())
    except ValueError:
        # Resolved target lives outside repo_root — should never
        # happen because we route through resolve_inside, but be
        # defensive and return the basename so we never leak an
        # absolute path into the response.
        return target.name
    return rel.as_posix() or "."


# ----------------------------------------------------------------------
# read_file
# ----------------------------------------------------------------------


@wrap_tool
async def read_file(
    app: GhiaApp,
    path: str,
    *,
    max_bytes: int = 1_000_000,
) -> ToolResponse:
    """Read a UTF-8 text file under ``app.repo_root``.

    Returns ``{path, content, size, encoding}``.  Files larger than
    ``max_bytes`` are refused with ``INVALID_INPUT`` — callers can
    raise the cap explicitly when they really need a big file.

    Non-UTF-8 bytes are decoded with ``errors="replace"`` and the
    response carries a ``warning`` field so callers can surface the
    degradation.  We deliberately don't fail on encoding errors: code
    files with stray latin-1 bytes are common and a "show me the file"
    tool that refuses such files is more annoying than useful.
    """

    try:
        target = resolve_inside(app.repo_root, path)
    except PathTraversalError as exc:
        return _path_traversal_err(exc)

    if not target.exists():
        return err(ErrorCode.FILE_NOT_FOUND, f"file not found: {path}")
    if not target.is_file():
        return err(ErrorCode.INVALID_INPUT, f"path is not a regular file: {path}")

    size = _safe_size(target)
    if size > max_bytes:
        return err(
            ErrorCode.INVALID_INPUT,
            f"file too large ({size} bytes > {max_bytes}); "
            f"pass max_bytes to override",
        )

    raw = await asyncio.to_thread(target.read_bytes)
    payload: dict[str, Any] = {
        "path": _rel(app.repo_root, target),
        "size": size,
        "encoding": "utf-8",
    }
    try:
        payload["content"] = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Best-effort decode — replace lets the caller still see most
        # of the file's text content for context.
        payload["content"] = raw.decode("utf-8", errors="replace")
        payload["warning"] = "non-utf8 content; replaced invalid bytes"
    return ok(payload)


# ----------------------------------------------------------------------
# write_file
# ----------------------------------------------------------------------


@wrap_tool
async def write_file(
    app: GhiaApp,
    path: str,
    content: str,
) -> ToolResponse:
    """Atomically write UTF-8 ``content`` to ``path`` under repo_root.

    Parent directories are created on demand (after the path-guard
    has confirmed the target lives inside repo_root).  The actual
    write goes through :func:`ghia.atomic.atomic_write_text` so a
    crash mid-write leaves the original file intact.

    Returns ``{path, bytes_written}`` — bytes counted on the encoded
    UTF-8 form, matching what landed on disk.
    """

    if not isinstance(content, str):
        return err(
            ErrorCode.INVALID_INPUT,
            "content must be a string",
        )

    try:
        target = resolve_inside(app.repo_root, path)
    except PathTraversalError as exc:
        return _path_traversal_err(exc)

    # Refuse to overwrite a non-file in place.  We don't want
    # ``write_file("some_dir", "...")`` to nuke a directory.
    if target.exists() and not target.is_file():
        return err(
            ErrorCode.INVALID_INPUT,
            f"refusing to write: path exists and is not a regular file: {path}",
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(atomic_write_text, target, content)
    return ok({
        "path": _rel(app.repo_root, target),
        "bytes_written": len(content.encode("utf-8")),
    })


# ----------------------------------------------------------------------
# list_directory
# ----------------------------------------------------------------------


@wrap_tool
async def list_directory(
    app: GhiaApp,
    path: str = ".",
    *,
    include_hidden: bool = False,
) -> ToolResponse:
    """Shallow listing of entries directly under ``path``.

    Returns ``{path, entries: [{name, type, size}]}`` sorted by name.
    Hidden entries (leading-dot) are filtered out by default — pass
    ``include_hidden=True`` for a forensic listing.
    """

    try:
        target = resolve_inside(app.repo_root, path)
    except PathTraversalError as exc:
        return _path_traversal_err(exc)

    if not target.exists():
        return err(ErrorCode.FILE_NOT_FOUND, f"directory not found: {path}")
    if not target.is_dir():
        return err(ErrorCode.INVALID_INPUT, f"path is not a directory: {path}")

    def _scan() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for child in sorted(target.iterdir(), key=lambda p: p.name):
            if not include_hidden and child.name.startswith("."):
                continue
            out.append({
                "name": child.name,
                "type": _classify(child),
                "size": _safe_size(child),
            })
        return out

    entries = await asyncio.to_thread(_scan)
    return ok({
        "path": _rel(app.repo_root, target),
        "entries": entries,
    })


# ----------------------------------------------------------------------
# search_codebase
# ----------------------------------------------------------------------


def _iter_search_files(repo_root: Path, glob: str) -> list[Path]:
    """Return candidate files matching ``glob`` with skip-dirs filtered.

    We materialize the list rather than yielding so the heavy I/O
    happens in one ``to_thread`` call instead of bouncing between
    threads on every iteration.
    """

    candidates: list[Path] = []
    for p in repo_root.rglob(glob):
        if not p.is_file():
            continue
        # Reject anything whose ancestry contains a skip-dir name.
        # rglob("**/*") includes paths inside .git etc., so we must
        # actively prune them here.
        try:
            rel_parts = p.resolve().relative_to(repo_root.resolve()).parts
        except ValueError:
            # Symlink that escapes — skip silently.
            continue
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        # Skip files larger than the heuristic threshold (probably
        # binary or generated; grepping them blows up output).
        if _safe_size(p) > _SEARCH_MAX_FILE_BYTES:
            continue
        candidates.append(p)
    return candidates


def _scan_file_for_query(
    path: Path, query: str, repo_root: Path
) -> list[dict[str, Any]]:
    """Return per-line match dicts for ``query`` in ``path``.

    Uses ``errors="replace"`` because mixed-encoding source files
    shouldn't crash the search.  Per-line iteration keeps memory bound
    to one line at a time even for largish files.
    """

    matches: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, start=1):
                if query in line:
                    matches.append({
                        "path": _rel(repo_root, path),
                        "line": lineno,
                        # Strip newline but preserve interior spaces.
                        "text": line.rstrip("\n"),
                    })
    except OSError as exc:
        # I/O hiccup on a single file shouldn't kill the whole search.
        logger.debug("search skipped %s: %s", path, exc)
    return matches


@wrap_tool
async def search_codebase(
    app: GhiaApp,
    query: str,
    *,
    glob: str = "**/*",
    max_matches: int = 200,
) -> ToolResponse:
    """Plain-text substring search over the repo.

    Skips ``.git/``, ``node_modules/``, ``__pycache__/``, ``.venv/``,
    ``dist/``, ``build/`` directories at any depth.  Skips files
    larger than 1 MB (heuristic for binary/generated content).

    Returns ``{query, matches, total_matches, truncated}`` where
    ``truncated`` is true when the actual hit count exceeded
    ``max_matches`` and the list was capped.
    """

    if not isinstance(query, str) or not query:
        return err(
            ErrorCode.INVALID_INPUT,
            "query must be a non-empty string",
        )
    if max_matches <= 0:
        return err(
            ErrorCode.INVALID_INPUT,
            "max_matches must be positive",
        )

    repo_root = app.repo_root

    def _do_search() -> tuple[list[dict[str, Any]], int]:
        matches: list[dict[str, Any]] = []
        total = 0
        for path in _iter_search_files(repo_root, glob):
            for hit in _scan_file_for_query(path, query, repo_root):
                total += 1
                if len(matches) < max_matches:
                    matches.append(hit)
        return matches, total

    matches, total = await asyncio.to_thread(_do_search)
    return ok({
        "query": query,
        "matches": matches,
        "total_matches": total,
        "truncated": total > max_matches,
    })


# ----------------------------------------------------------------------
# get_repo_structure
# ----------------------------------------------------------------------


@wrap_tool
async def get_repo_structure(
    app: GhiaApp,
    *,
    max_depth: int = 3,
) -> ToolResponse:
    """Return a depth-limited flat list of paths in the repo.

    Same skip-list as :func:`search_codebase`.  Depth is counted from
    repo_root: depth=1 means top-level entries only.  We return a flat
    list (not a tree) because consumers can re-build the tree
    trivially and a flat list is much friendlier to JSON transport.
    """

    if max_depth <= 0:
        return err(
            ErrorCode.INVALID_INPUT,
            "max_depth must be positive",
        )

    repo_root = app.repo_root
    root_resolved = repo_root.resolve()

    def _walk() -> list[str]:
        # ``os.walk`` lets us mutate ``dirnames`` in place to prune
        # subtrees, which is much cheaper than relying on rglob and
        # filtering after the fact.
        #
        # ``depth`` here is the depth of ``dirpath`` itself relative
        # to repo_root (root = 0).  Children of ``dirpath`` are at
        # ``depth + 1``, so we stop descending once ``depth + 1``
        # would exceed ``max_depth``.  We DO still emit the children
        # at ``depth + 1`` from the current frame's filenames/dirnames.
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root_resolved):
            depth = len(Path(dirpath).resolve().relative_to(root_resolved).parts)
            # Prune skip-dirs so we don't even descend into them.
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]

            for fname in sorted(filenames):
                out.append(_rel(repo_root, Path(dirpath) / fname))
            for dname in sorted(dirnames):
                out.append(_rel(repo_root, Path(dirpath) / dname) + "/")

            # Stop descending if the *next* level would exceed max_depth.
            if depth + 1 >= max_depth:
                dirnames[:] = []
        return sorted(set(out))

    tree = await asyncio.to_thread(_walk)
    return ok({
        "root": _rel(repo_root, repo_root),
        "tree": tree,
    })


# ----------------------------------------------------------------------
# read_multiple_files
# ----------------------------------------------------------------------


@wrap_tool
async def read_multiple_files(
    app: GhiaApp,
    paths: list[str],
) -> ToolResponse:
    """Batch read; per-file errors don't fail the call.

    Returns ``{files: [{path, content?, error?}]}`` — successful reads
    carry ``content``; failures carry ``error`` (string description).
    The whole call only fails if ``paths`` itself is malformed.
    """

    if not isinstance(paths, list):
        return err(
            ErrorCode.INVALID_INPUT,
            "paths must be a list of strings",
        )

    results: list[dict[str, Any]] = []
    for raw_path in paths:
        if not isinstance(raw_path, str):
            results.append({
                "path": str(raw_path),
                "error": "path entry must be a string",
            })
            continue
        try:
            target = resolve_inside(app.repo_root, raw_path)
        except PathTraversalError as exc:
            results.append({"path": raw_path, "error": f"path_traversal: {exc}"})
            continue
        if not target.exists() or not target.is_file():
            results.append({"path": raw_path, "error": "file_not_found"})
            continue
        try:
            data = await asyncio.to_thread(target.read_bytes)
            results.append({
                "path": _rel(app.repo_root, target),
                "content": data.decode("utf-8", errors="replace"),
            })
        except OSError as exc:
            # Per-file OS error: bubble up as a per-entry error
            # without failing the whole batch.
            results.append({"path": raw_path, "error": f"io_error: {exc}"})

    return ok({"files": results})
