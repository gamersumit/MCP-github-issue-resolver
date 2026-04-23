"""Atomic file-write helpers (TRD-004).

Writes go through a sibling tempfile ``{path}.tmp.{pid}.{ts}`` that is
flushed + fsynced before being ``os.replace``-d into place.  This
guarantees readers see either the full old content or the full new
content, never a partial mix.

Satisfies REQ-015.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

__all__ = ["atomic_write_text", "atomic_write_bytes"]


def _tmp_path(target: Path) -> Path:
    """Produce the sibling temp path for ``target``.

    Format: ``{target}.tmp.{pid}.{ns}`` — using ``time.monotonic_ns`` so
    two calls in the same process don't collide even when wall-clock
    resolution is coarse.
    """

    stamp = time.monotonic_ns()
    return target.with_name(f"{target.name}.tmp.{os.getpid()}.{stamp}")


def _preserve_mode(target: Path, tmp: Path) -> None:
    """Copy the existing target's POSIX mode bits onto the tempfile.

    No-op on Windows (``os.chmod`` has limited effect) and no-op when
    the target doesn't exist yet — new files inherit the umask.
    """

    if os.name != "posix":
        return
    try:
        if target.exists():
            mode = target.stat().st_mode & 0o777
            os.chmod(tmp, mode)
    except OSError as exc:
        # Non-fatal: if we can't read/copy the mode, log and move on.
        logger.debug("could not preserve mode for %s: %s", target, exc)


def _cleanup_tmp(tmp: Path) -> None:
    try:
        if tmp.exists():
            tmp.unlink()
    except OSError as exc:
        logger.warning("failed to clean up temp file %s: %s", tmp, exc)


def _write_and_sync(tmp: Path, payload: Union[str, bytes]) -> None:
    """Write ``payload`` to ``tmp`` and fsync before the file is closed.

    We deliberately open the file, write, flush, fsync, *then* close —
    fsync-before-close is the invariant that matters for durability.
    """

    if isinstance(payload, str):
        # We want deterministic byte-level behavior on-disk (newlines,
        # encoding); so we encode ourselves and use a binary handle.
        data: bytes = payload.encode("utf-8")
    else:
        data = payload

    # Opening with mode "wb" so we control the bytes exactly.  The
    # file is created with 0o666 minus umask — caller may chmod later.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
    try:
        with os.fdopen(fd, "wb", closefd=True) as fh:
            fh.write(data)
            fh.flush()
            if os.name == "posix":
                os.fsync(fh.fileno())
    except Exception:
        # os.fdopen closed the fd; re-raise so the outer writer handles
        # cleanup of the (possibly partial) tempfile.
        raise


def atomic_write_text(path: Union[str, os.PathLike[str]], content: str) -> None:
    """Atomically write ``content`` to ``path`` as UTF-8 text.

    On POSIX: data is fsynced before rename, so a crash mid-write leaves
    the original file intact.  Permissions of an existing ``path`` are
    preserved.

    Args:
        path: Destination file path; may be any path-like.
        content: Text to write.  Encoded as UTF-8.

    Raises:
        OSError: propagated if the tempfile cannot be written or the
            atomic rename fails.  In either case the tempfile is
            cleaned up if it exists.
    """

    target = Path(os.fspath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(target)
    try:
        _write_and_sync(tmp, content)
        _preserve_mode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        _cleanup_tmp(tmp)
        raise


def atomic_write_bytes(
    path: Union[str, os.PathLike[str]], bytes_: bytes
) -> None:
    """Atomically write ``bytes_`` to ``path``.

    Binary twin of :func:`atomic_write_text` with identical guarantees.
    """

    target = Path(os.fspath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(target)
    try:
        _write_and_sync(tmp, bytes_)
        _preserve_mode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        _cleanup_tmp(tmp)
        raise
