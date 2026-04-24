"""Git MCP tools + default-branch detection (TRD-023).

Six user-visible tools that wrap ``git`` via ``subprocess.run``,
off-loaded onto a worker thread with :func:`asyncio.to_thread` so the
event loop never blocks on a slow fork or fsync:

* ``get_default_branch`` — discover the upstream default (``main`` /
  ``master`` / ``develop`` / ...) and cache it on the session
* ``get_current_branch`` — current branch (or literal ``"HEAD"`` when
  detached)
* ``create_branch`` — create + switch with strict-name validation and
  ``-v2``..``-v9`` collision suffixing
* ``git_diff`` — unified diff (worktree or staged) optionally scoped
  to paths
* ``commit_changes`` — stage + commit with default-branch protection
* ``push_branch`` — push (optionally setting upstream) with
  default-branch protection

**Subprocess discipline (TRD §2.5):**

* ``shell=False`` always — argv lists, never strings.
* 30-second timeout on every git invocation.
* ``FileNotFoundError`` → ``GIT_NOT_FOUND`` (the binary is missing).
* Non-zero return code → ``GIT_ERROR`` with stderr included.
* No env vars or repo absolute paths in the response payload.

Satisfies REQ-016.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from ghia.app import GhiaApp
from ghia.errors import ErrorCode, ToolResponse, err, ok, wrap_tool

logger = logging.getLogger(__name__)

__all__ = [
    "get_default_branch",
    "get_current_branch",
    "create_branch",
    "git_diff",
    "commit_changes",
    "push_branch",
]


# Branch name validator — letters, digits, dot, underscore, slash,
# dash.  No spaces, no shell metas, no leading dash (the ``+`` quantifier
# accepts a leading dash but git itself will reject it later, which is
# fine; the regex's job is to keep the argv shell-safe and predictable).
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

# Candidates probed in order when no remote/HEAD is available.  Order
# matters: ``main`` is the modern default, ``master`` is legacy, the
# others are common but rarer.
_DEFAULT_BRANCH_CANDIDATES: tuple[str, ...] = ("main", "master", "develop", "trunk")

# Hard cap on subprocess wall-time.  Most git ops finish in <1s; 30s
# is generous enough that a slow disk doesn't trip us up but short
# enough that we don't hang the event loop indefinitely.
_GIT_TIMEOUT_S = 30

# Maximum collision-suffix index for create_branch.  -v2 through -v9
# inclusive — by the time you've collided 9 times something is wrong
# upstream and we should fail loudly rather than keep guessing.
_MAX_COLLISION_SUFFIX = 9


# ----------------------------------------------------------------------
# Subprocess helper
# ----------------------------------------------------------------------


async def _run_git(
    app: GhiaApp,
    *args: str,
    cwd: Optional[Path] = None,
) -> tuple[int, str, str]:
    """Run ``git <args>`` via :func:`asyncio.to_thread` — non-blocking.

    Returns ``(returncode, stdout, stderr)``.  The caller is
    responsible for translating non-zero returncodes into structured
    errors — this helper just shuttles bytes across the thread
    boundary.

    Raises:
        FileNotFoundError: if the ``git`` binary isn't on ``PATH``.
            Callers wrap this into ``GIT_NOT_FOUND``.
        subprocess.TimeoutExpired: if the call exceeds ``_GIT_TIMEOUT_S``.
            Callers wrap this into ``GIT_ERROR``.
    """

    cwd = cwd or app.repo_root

    def _call() -> subprocess.CompletedProcess[str]:
        # ``shell=False`` (default) is critical: every arg goes through
        # the kernel as-is, no quoting/escaping pitfalls.
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_S,
        )

    proc = await asyncio.to_thread(_call)
    return proc.returncode, proc.stdout, proc.stderr


def _git_error(args: tuple[str, ...], rc: int, stderr: str) -> ToolResponse:
    """Build a ``GIT_ERROR`` response, redacting full paths from stderr.

    We don't include ``cwd`` or environment in the error payload — the
    full repo path can leak into transcripts and is not the caller's
    business.
    """

    cmd_preview = " ".join(["git", *args])
    msg = stderr.strip() or f"git exited with code {rc}"
    return err(ErrorCode.GIT_ERROR, f"{cmd_preview!r} failed: {msg}")


def _git_not_found_err() -> ToolResponse:
    """Uniform error when the git binary itself is missing."""

    return err(
        ErrorCode.GIT_NOT_FOUND,
        "git executable not found on PATH",
    )


async def _try_run_git(
    app: GhiaApp, *args: str
) -> tuple[Optional[int], str, str, Optional[ToolResponse]]:
    """Like :func:`_run_git` but returns a structured error instead of raising.

    Returns ``(rc, stdout, stderr, err_response)`` — exactly one of
    ``rc`` or ``err_response`` is ``None``.  Callers branch on which
    one was set.
    """

    try:
        rc, out, errout = await _run_git(app, *args)
    except FileNotFoundError:
        return None, "", "", _git_not_found_err()
    except subprocess.TimeoutExpired as exc:
        return None, "", "", err(
            ErrorCode.GIT_ERROR,
            f"git timed out after {_GIT_TIMEOUT_S}s: {exc.cmd!r}",
        )
    return rc, out, errout, None


# ----------------------------------------------------------------------
# get_default_branch
# ----------------------------------------------------------------------


async def _detect_default_branch(app: GhiaApp) -> ToolResponse:
    """Multi-strategy detection of the repo's default branch.

    Order of attempts (TRD-023 spec):

    1. ``git symbolic-ref refs/remotes/origin/HEAD`` — fast path when
       the remote was cloned with ``-o origin`` (the default).
    2. ``git remote show origin`` — slower but works when the symbolic
       ref isn't set locally; we parse the ``HEAD branch:`` line.
    3. Probe local ``refs/heads/{candidate}`` for ``main``, ``master``,
       ``develop``, ``trunk`` — for repos with no remote at all.
    4. Otherwise: ``NO_DEFAULT_BRANCH_DETECTED``.
    """

    # Strategy 1: symbolic-ref of origin's HEAD.
    rc, out, _, err_resp = await _try_run_git(
        app, "symbolic-ref", "refs/remotes/origin/HEAD"
    )
    if err_resp is not None:
        # GIT_NOT_FOUND short-circuits everything — no point trying
        # the other strategies if the binary isn't installed.
        if err_resp.code == ErrorCode.GIT_NOT_FOUND:
            return err_resp
    elif rc == 0:
        ref = out.strip()
        prefix = "refs/remotes/origin/"
        if ref.startswith(prefix):
            return ok({"default_branch": ref[len(prefix):]})

    # Strategy 2: parse ``git remote show origin``.  This shells out to
    # the remote, which can be slow over a network — but we only get
    # here when strategy 1 failed, and the result gets cached.
    rc, out, _, err_resp = await _try_run_git(app, "remote", "show", "origin")
    if err_resp is not None and err_resp.code == ErrorCode.GIT_NOT_FOUND:
        return err_resp
    if err_resp is None and rc == 0:
        for line in out.splitlines():
            stripped = line.strip()
            # Format: "  HEAD branch: main"
            if stripped.startswith("HEAD branch:"):
                branch = stripped.split(":", 1)[1].strip()
                if branch and branch != "(unknown)":
                    return ok({"default_branch": branch})

    # Strategy 3: no remote — probe local heads in priority order.
    for candidate in _DEFAULT_BRANCH_CANDIDATES:
        rc, _, _, err_resp = await _try_run_git(
            app, "show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"
        )
        if err_resp is not None and err_resp.code == ErrorCode.GIT_NOT_FOUND:
            return err_resp
        if err_resp is None and rc == 0:
            return ok({"default_branch": candidate})

    return err(
        ErrorCode.NO_DEFAULT_BRANCH_DETECTED,
        "could not detect a default branch (no remote HEAD, no main/master/develop/trunk)",
    )


@wrap_tool
async def get_default_branch(app: GhiaApp) -> ToolResponse:
    """Return ``{default_branch}``, caching on ``app.session`` after first call.

    The cache lives in ``SessionState.default_branch`` — once detected,
    later calls skip the subprocess shelling entirely.  This matters
    because :func:`commit_changes` and :func:`push_branch` both need
    to know the default branch and would otherwise re-shell on every
    call.
    """

    # Cached path: read first, only fall through to detection when
    # missing.  Read is unlocked because SessionStore.read is a
    # snapshot.
    cached = await app.session.read()
    if cached.default_branch:
        return ok({"default_branch": cached.default_branch})

    resp = await _detect_default_branch(app)
    if resp.success:
        # Persist for next time.  ``update`` is the public API; it
        # takes the lock internally so concurrent detection calls are
        # safe.
        await app.session.update(default_branch=resp.data["default_branch"])
    return resp


# ----------------------------------------------------------------------
# get_current_branch
# ----------------------------------------------------------------------


@wrap_tool
async def get_current_branch(app: GhiaApp) -> ToolResponse:
    """Return ``{current_branch}`` — literal ``"HEAD"`` when detached.

    ``git rev-parse --abbrev-ref HEAD`` prints ``HEAD`` when no branch
    is checked out (detached state).  We surface that string rather
    than synthesizing a placeholder — callers that care can compare
    against the literal ``"HEAD"``.
    """

    rc, out, errout, err_resp = await _try_run_git(
        app, "rev-parse", "--abbrev-ref", "HEAD"
    )
    if err_resp is not None:
        return err_resp
    if rc != 0:
        return _git_error(("rev-parse", "--abbrev-ref", "HEAD"), rc, errout)
    return ok({"current_branch": out.strip()})


# ----------------------------------------------------------------------
# create_branch
# ----------------------------------------------------------------------


async def _branch_exists(app: GhiaApp, name: str) -> tuple[Optional[bool], Optional[ToolResponse]]:
    """Check whether ``refs/heads/{name}`` exists locally.

    Returns ``(exists?, err_response)`` — exactly one is non-None.
    Used by :func:`create_branch` to drive collision-suffix retries.
    """

    rc, _, errout, err_resp = await _try_run_git(
        app, "show-ref", "--verify", "--quiet", f"refs/heads/{name}"
    )
    if err_resp is not None:
        return None, err_resp
    if rc == 0:
        return True, None
    if rc == 1:
        # show-ref --verify returns 1 when the ref doesn't exist —
        # exactly the "absent" signal we want.
        return False, None
    # Any other rc is a real error (corrupted repo, permissions, ...).
    return None, _git_error(
        ("show-ref", "--verify", "--quiet", f"refs/heads/{name}"), rc, errout
    )


@wrap_tool
async def create_branch(
    app: GhiaApp,
    name: str,
    *,
    base: Optional[str] = None,
) -> ToolResponse:
    """Create branch ``name`` (with collision-suffixing) and switch to it.

    Validation: ``name`` must match ``[A-Za-z0-9._/-]+`` — no spaces,
    no shell metas.  This is stricter than git's own refname grammar
    on purpose: keeping the alphabet small means the name never needs
    quoting and is safe to interpolate into commit messages, PR
    titles, etc.

    Collision: if the requested name already exists locally, we try
    ``-v2``, ``-v3``, ..., up to ``-v9`` before giving up with
    ``BRANCH_EXISTS``.  The returned ``branch`` field is the actual
    name that landed on disk.

    ``base`` defaults to ``None`` → git uses HEAD.  Callers that want
    the new branch rooted at ``main`` should pass ``base="main"``
    explicitly.
    """

    if not isinstance(name, str) or not _BRANCH_NAME_RE.match(name):
        return err(
            ErrorCode.INVALID_INPUT,
            f"invalid branch name {name!r}; "
            f"must match [A-Za-z0-9._/-]+ (no spaces or shell metas)",
        )

    # Collision-suffix loop.  We probe locally only — origin-side
    # collisions are surfaced by ``git push`` later if they matter.
    candidate = name
    for attempt in range(0, _MAX_COLLISION_SUFFIX):
        if attempt > 0:
            candidate = f"{name}-v{attempt + 1}"

        exists, err_resp = await _branch_exists(app, candidate)
        if err_resp is not None:
            return err_resp
        if exists:
            continue

        # Found a free name — try to create + switch in one shot.
        argv: list[str] = ["switch", "-c", candidate]
        if base:
            argv.append(base)
        rc, _, errout, err_resp = await _try_run_git(app, *argv)
        if err_resp is not None:
            return err_resp
        if rc != 0:
            return _git_error(tuple(argv), rc, errout)
        return ok({"branch": candidate, "created": True})

    return err(
        ErrorCode.BRANCH_EXISTS,
        f"branch {name!r} and -v2..-v{_MAX_COLLISION_SUFFIX} all exist; refusing",
    )


# ----------------------------------------------------------------------
# git_diff
# ----------------------------------------------------------------------


def _count_diff_files(diff_text: str) -> int:
    """Count files in a unified diff by counting ``diff --git`` lines.

    Cheap heuristic that works for the standard ``git diff`` output;
    we don't try to be clever about renames or mode changes here.
    """

    if not diff_text:
        return 0
    return sum(1 for line in diff_text.splitlines() if line.startswith("diff --git "))


@wrap_tool
async def git_diff(
    app: GhiaApp,
    *,
    staged: bool = False,
    paths: Optional[list[str]] = None,
) -> ToolResponse:
    """Return unified diff (``{diff, files_changed}``).

    ``staged=True`` runs ``git diff --cached`` (index vs HEAD); the
    default runs worktree vs index.  ``paths`` is forwarded after a
    ``--`` separator so leading-dash filenames don't get mistaken for
    flags.
    """

    argv: list[str] = ["diff"]
    if staged:
        argv.append("--cached")
    if paths:
        if not all(isinstance(p, str) for p in paths):
            return err(
                ErrorCode.INVALID_INPUT,
                "paths must be a list of strings",
            )
        argv.append("--")
        argv.extend(paths)

    rc, out, errout, err_resp = await _try_run_git(app, *argv)
    if err_resp is not None:
        return err_resp
    if rc != 0:
        return _git_error(tuple(argv), rc, errout)
    return ok({
        "diff": out,
        "files_changed": _count_diff_files(out),
    })


# ----------------------------------------------------------------------
# commit_changes
# ----------------------------------------------------------------------


async def _resolve_default_and_current(
    app: GhiaApp,
) -> tuple[Optional[str], Optional[str], Optional[ToolResponse]]:
    """Pull both ``default_branch`` and ``current_branch`` once.

    Returns ``(default, current, err_response)`` — err_response is
    non-None on any failure.  Used by ``commit_changes`` and
    ``push_branch`` to enforce the "never mutate default branch" rule.
    """

    default_resp = await get_default_branch(app)
    if not default_resp.success:
        return None, None, default_resp
    default = default_resp.data["default_branch"]

    current_resp = await get_current_branch(app)
    if not current_resp.success:
        return None, None, current_resp
    current = current_resp.data["current_branch"]

    return default, current, None


@wrap_tool
async def commit_changes(
    app: GhiaApp,
    message: str,
    *,
    paths: Optional[list[str]] = None,
) -> ToolResponse:
    """Stage and commit.  Refuses on the default branch.

    Staging policy:
      * ``paths`` given → ``git add -- <paths>``  (explicit list)
      * ``paths`` omitted → ``git add -u``        (modified-tracked only)

    We deliberately never use ``git add -A`` — that would silently
    sweep in untracked junk like editor swap files or build outputs.
    Caller-supplied paths still go through git's own path validation.

    The "default branch" check uses :func:`get_default_branch` (which
    is cached on the session) so we don't pay the detection cost on
    every commit.

    Returns ``{sha, message, files_changed}`` from the post-commit
    HEAD.
    """

    if not isinstance(message, str) or not message.strip():
        return err(
            ErrorCode.INVALID_INPUT,
            "commit message must be a non-empty string",
        )

    default, current, err_resp = await _resolve_default_and_current(app)
    if err_resp is not None:
        return err_resp
    if current == default:
        return err(
            ErrorCode.ON_DEFAULT_BRANCH_REFUSED,
            f"refusing to commit on default branch {default!r}; "
            f"create a feature branch first",
        )

    # Stage.  The two arms are deliberately distinct: -u touches only
    # already-tracked files, while explicit paths can include new
    # files (which is what callers want when they pass paths).
    if paths:
        if not all(isinstance(p, str) for p in paths):
            return err(
                ErrorCode.INVALID_INPUT,
                "paths must be a list of strings",
            )
        add_argv: list[str] = ["add", "--", *paths]
    else:
        add_argv = ["add", "-u"]
    rc, _, errout, err_resp = await _try_run_git(app, *add_argv)
    if err_resp is not None:
        return err_resp
    if rc != 0:
        return _git_error(tuple(add_argv), rc, errout)

    # Count *staged* changes BEFORE committing — after the commit they
    # vanish from the index.  diff --cached --numstat is the cheapest
    # way; we count lines of output.
    rc, num_out, _, err_resp = await _try_run_git(
        app, "diff", "--cached", "--numstat"
    )
    if err_resp is not None:
        return err_resp
    files_changed = (
        sum(1 for line in num_out.splitlines() if line.strip())
        if rc == 0
        else 0
    )
    if files_changed == 0:
        return err(
            ErrorCode.INVALID_INPUT,
            "nothing staged to commit",
        )

    # Honour the user's git config for author/committer; we never pass
    # --author so identity stays under operator control.
    commit_argv: list[str] = ["commit", "-m", message]
    rc, _, errout, err_resp = await _try_run_git(app, *commit_argv)
    if err_resp is not None:
        return err_resp
    if rc != 0:
        return _git_error(tuple(commit_argv), rc, errout)

    rc, sha_out, errout, err_resp = await _try_run_git(app, "rev-parse", "HEAD")
    if err_resp is not None:
        return err_resp
    if rc != 0:
        return _git_error(("rev-parse", "HEAD"), rc, errout)

    return ok({
        "sha": sha_out.strip(),
        "message": message,
        "files_changed": files_changed,
    })


# ----------------------------------------------------------------------
# push_branch
# ----------------------------------------------------------------------


@wrap_tool
async def push_branch(
    app: GhiaApp,
    *,
    remote: str = "origin",
    set_upstream: bool = True,
) -> ToolResponse:
    """Push current HEAD to ``remote``.  Refuses on the default branch.

    ``set_upstream=True`` (the default) adds ``-u`` so subsequent
    plain ``git push`` calls work without arguments.
    """

    if not isinstance(remote, str) or not _BRANCH_NAME_RE.match(remote):
        return err(
            ErrorCode.INVALID_INPUT,
            f"invalid remote name {remote!r}",
        )

    default, current, err_resp = await _resolve_default_and_current(app)
    if err_resp is not None:
        return err_resp
    if current == default:
        return err(
            ErrorCode.ON_DEFAULT_BRANCH_REFUSED,
            f"refusing to push default branch {default!r} from this tool",
        )

    argv: list[str] = ["push"]
    if set_upstream:
        argv.append("-u")
    argv.extend([remote, "HEAD"])

    rc, out, errout, err_resp = await _try_run_git(app, *argv)
    if err_resp is not None:
        return err_resp
    if rc != 0:
        return _git_error(tuple(argv), rc, errout)

    # git push prints progress to stderr, not stdout — return both
    # streams concatenated so callers see the full transcript.
    return ok({
        "remote": remote,
        "branch": current,
        "output": (out + errout).strip(),
    })
