"""Path-traversal guard utility (TRD-005).

Any filesystem tool that accepts caller-supplied paths must route them
through :func:`resolve_inside` before any I/O.  This module rejects:

* absolute paths that point outside ``repo_root``
* relative paths whose resolved form escapes ``repo_root`` (``..``
  traversal)
* paths whose real target after symlink resolution escapes
  ``repo_root``

Satisfies REQ-014.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union

__all__ = ["PathTraversalError", "resolve_inside"]


class PathTraversalError(Exception):
    """Raised when a candidate path would escape ``repo_root``.

    The optional ``attempted`` attribute carries the original string
    form of the offending candidate, which lets callers build a
    structured error response without re-parsing the message.
    """

    def __init__(self, message: str, attempted: Optional[str] = None) -> None:
        super().__init__(message)
        self.attempted = attempted


def resolve_inside(
    repo_root: Union[str, os.PathLike[str]],
    candidate: Union[str, os.PathLike[str]],
) -> Path:
    """Resolve ``candidate`` against ``repo_root`` and verify containment.

    The returned path is fully resolved (symlinks followed, ``..``
    collapsed) and guaranteed to live inside ``repo_root``.

    Args:
        repo_root: Directory that the candidate must live inside.  May
            or may not exist — it is resolved with ``strict=False``.
        candidate: A relative or absolute path supplied by a tool
            caller.  Relative paths are joined onto ``repo_root``.

    Returns:
        The resolved :class:`pathlib.Path` (absolute).

    Raises:
        PathTraversalError: if the resolved path escapes ``repo_root``
            or the candidate is empty / malformed.
    """

    attempted = os.fspath(candidate)
    if not attempted:
        raise PathTraversalError(
            "empty path is not a valid candidate", attempted=attempted
        )

    root = Path(os.fspath(repo_root)).resolve(strict=False)
    cand = Path(attempted)

    if cand.is_absolute():
        resolved = cand.resolve(strict=False)
    else:
        resolved = (root / cand).resolve(strict=False)

    # is_relative_to is 3.9+; we support 3.10+.  Using it rather than
    # commonpath because it handles case-sensitivity correctly on the
    # host OS and gives a clean True/False answer.
    if not resolved.is_relative_to(root):
        raise PathTraversalError(
            f"path {attempted!r} escapes repo_root {str(root)!r} "
            f"(resolved to {str(resolved)!r})",
            attempted=attempted,
        )

    return resolved
