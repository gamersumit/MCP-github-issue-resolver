"""Linting tool (TRD-025).

One MCP tool — :func:`check_linting` — that runs the configured
linter against the *changed* files only.  We deliberately don't lint
the entire repo: the agent should be a good citizen and only act on
its own diffs, not flag pre-existing issues that the user never
wanted touched.

**Subprocess discipline (TRD §2.5):**

* The configured ``lint_command`` was already vetted by the
  allow-list at config-load time (:mod:`ghia.tools.validation`).  We
  re-validate here as defense-in-depth — a corrupted in-memory
  ``Config`` should still not be able to escape the allow-list.
* ``shell=False`` always; the command is ``shlex.split`` then the
  changed-file paths are appended as separate argv entries.
* 5-minute wall-clock cap so a runaway linter doesn't hang the
  event loop.
* ``FileNotFoundError`` on the linter binary → ``INVALID_INPUT``
  with the binary name, not a stack trace.

The "changed files" are derived from ``git diff --name-only HEAD``.
Deleted files are filtered out before the linter runs (most linters
crash on a missing path).

Satisfies REQ-017b.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import subprocess
from typing import Any

from ghia.app import GhiaApp
from ghia.errors import ErrorCode, ToolResponse, err, ok, wrap_tool
from ghia.tools.git import _run_git
from ghia.tools.validation import InvalidCommandError, validate_command

logger = logging.getLogger(__name__)

__all__ = ["check_linting"]


# Linters can be slow on big diffs (eslint on a 200-file PR can take
# minutes) but anything past five minutes is a runaway and the event
# loop should be allowed to move on.
_LINT_TIMEOUT_S = 300


@wrap_tool
async def check_linting(app: GhiaApp) -> ToolResponse:
    """Lint only the changed files; return structured pass/fail.

    Behaviour matrix:

    * No ``lint_command`` configured → ``ok({skipped: true, ...})``
      (lint is opt-in; absence is not an error).
    * ``git diff --name-only HEAD`` empty → ``ok({linted: [], ...,
      skipped_no_changes: true})`` (nothing to lint is a success).
    * Linter binary missing → ``err(INVALID_INPUT, ...)`` with the
      binary name.
    * Linter ran → ``ok({passed, command, files, stdout, stderr,
      returncode})`` where ``passed = (returncode == 0)``.

    The whole flow returns ``ok(...)`` even for "lint failed";
    callers branch on ``data.passed``.  ``TEST_FAILED`` is reserved
    for unrecoverable infrastructure errors and isn't applicable
    here — a lint failure is still a successful tool invocation that
    happened to find issues.
    """

    cmd_str = app.config.lint_command
    if not cmd_str or not cmd_str.strip():
        # Lint is optional — silently skip if the user didn't set it.
        return ok({"skipped": True, "reason": "no lint command configured"})

    # Defense-in-depth: even though config validation ran at load
    # time, we re-validate here so an in-memory mutation of Config
    # cannot bypass the allow-list.
    try:
        validate_command(cmd_str, kind="lint")
    except InvalidCommandError as exc:
        return err(ErrorCode.INVALID_INPUT, str(exc))

    # Resolve the set of changed files from the worktree HEAD.  We
    # use HEAD (not --cached) because the agent's edits land in the
    # worktree before being committed.
    try:
        rc, out, errout = await _run_git(app, "diff", "--name-only", "HEAD")
    except FileNotFoundError:
        return err(ErrorCode.INVALID_INPUT, "git executable not found on PATH")
    except subprocess.TimeoutExpired:
        return err(ErrorCode.INVALID_INPUT, "git diff timed out")
    if rc != 0:
        return err(
            ErrorCode.INVALID_INPUT,
            f"git diff failed: {errout.strip() or 'rc=' + str(rc)}",
        )

    # Filter to existing files only — deleted entries would crash
    # most linters with a "no such file" error.
    candidate_files = [line.strip() for line in out.splitlines() if line.strip()]
    changed_files = [
        rel for rel in candidate_files if (app.repo_root / rel).is_file()
    ]
    if not changed_files:
        return ok({
            "linted": [],
            "passed": True,
            "skipped_no_changes": True,
        })

    # Build argv: the allow-list already guarantees no shell metas,
    # but going through shlex.split is the standard way to honour
    # quoted args (e.g. ``ruff check --select=E,F``) without ever
    # invoking a shell.
    argv = shlex.split(cmd_str) + list(changed_files)

    def _call() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            cwd=str(app.repo_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=_LINT_TIMEOUT_S,
        )

    try:
        proc = await asyncio.to_thread(_call)
    except FileNotFoundError:
        # The first argv element (the linter binary) is missing.  We
        # report it explicitly so the user can fix their PATH or
        # install the tool.
        binary = argv[0] if argv else "<unknown>"
        return err(ErrorCode.INVALID_INPUT, f"lint executable not found: {binary}")
    except subprocess.TimeoutExpired:
        return err(
            ErrorCode.INVALID_INPUT,
            f"lint command timed out after {_LINT_TIMEOUT_S}s",
        )

    payload: dict[str, Any] = {
        "passed": proc.returncode == 0,
        "command": cmd_str,
        "files": changed_files,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "returncode": proc.returncode,
    }
    return ok(payload)
