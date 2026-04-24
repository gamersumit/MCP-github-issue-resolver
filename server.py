"""FastMCP entrypoint (TRD-011, v0.2 refactor).

Exposes the Cluster 3 control tools (``issue_agent_start``,
``issue_agent_stop``, ``issue_agent_status``, ``issue_agent_set_mode``,
``issue_agent_fetch_now``) to any MCP client.  The server is
idle-by-default: starting the process does NOT call
``issue_agent_start`` — the user has to ask for it explicitly.

The :class:`ghia.app.GhiaApp` instance is built lazily on first tool
call so that ``claude mcp list`` and ``claude mcp add ...`` succeed
even when the user has not yet run the setup wizard.  The lazy build
covers two structured-error paths:

* **Repo not detected** — the cwd isn't a git repo or has no origin.
  Surfaces as ``INVALID_INPUT`` with the detection error message.
* **Config missing** — the wizard hasn't been run for this repo.
  Surfaces as ``CONFIG_MISSING`` with a hint to run the wizard.

In either case the process keeps running so the next call (in a
different cwd, or after the wizard runs) can succeed.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

from fastmcp import FastMCP

from ghia.app import GhiaApp, create_app
from ghia.config import ConfigMissingError
from ghia.errors import ErrorCode, ToolResponse, err
from ghia.repo_detect import RepoDetectionError
from ghia.tools import control

logger = logging.getLogger(__name__)


mcp: FastMCP = FastMCP("github-issue-agent")

# ``_app`` is lazily initialized on the first tool call.  We also track
# whether initialization was already attempted and failed, so repeat
# calls in a config-less environment don't keep retrying I/O.
_app: Optional[GhiaApp] = None
_app_lock: asyncio.Lock = asyncio.Lock()


async def _get_app_or_error() -> tuple[Optional[GhiaApp], Optional[ToolResponse]]:
    """Return ``(app, None)`` on success or ``(None, error_response)``.

    Lazy so ``fastmcp`` can enumerate tools before the setup wizard has
    run.  The first caller pays the load cost; subsequent callers get
    the cached instance.
    """

    global _app
    if _app is not None:
        return _app, None

    async with _app_lock:
        if _app is not None:  # double-checked locking
            return _app, None
        try:
            _app = await create_app(repo_root=Path.cwd())
        except RepoDetectionError as exc:
            # Distinct from CONFIG_MISSING — the user opened Claude
            # Code somewhere that isn't a github-hosted git repo.
            # INVALID_INPUT is the closest existing error code.
            logger.info("repo detection failed: %s", exc)
            return None, err(ErrorCode.INVALID_INPUT, str(exc))
        except ConfigMissingError as exc:
            logger.info("create_app failed: %s", exc)
            return None, err(
                ErrorCode.CONFIG_MISSING,
                "No per-repo config for this repo. "
                "Run `python -m setup_wizard` from the repo dir to create one.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("unexpected error initializing app")
            return None, err(ErrorCode.INVALID_INPUT, str(exc))
    return _app, None


def _dump(resp: ToolResponse) -> dict[str, Any]:
    """Serialize a ToolResponse for the MCP transport."""

    return resp.model_dump(mode="json")


# ----------------------------------------------------------------------
# Tool registrations
# ----------------------------------------------------------------------


@mcp.tool()
async def issue_agent_start() -> dict[str, Any]:
    """Start the agent and render the active protocol."""

    app, error = await _get_app_or_error()
    if error is not None:
        return _dump(error)
    return _dump(await control.issue_agent_start(app))


@mcp.tool()
async def issue_agent_stop() -> dict[str, Any]:
    """Pause the agent without losing session history."""

    app, error = await _get_app_or_error()
    if error is not None:
        return _dump(error)
    return _dump(await control.issue_agent_stop(app))


@mcp.tool()
async def issue_agent_status() -> dict[str, Any]:
    """Return a snapshot of the current session state."""

    app, error = await _get_app_or_error()
    if error is not None:
        return _dump(error)
    return _dump(await control.issue_agent_status(app))


@mcp.tool()
async def issue_agent_set_mode(mode: str) -> dict[str, Any]:
    """Switch between ``semi`` and ``full`` mid-session."""

    app, error = await _get_app_or_error()
    if error is not None:
        return _dump(error)
    return _dump(await control.issue_agent_set_mode(app, mode))


@mcp.tool()
async def issue_agent_fetch_now() -> dict[str, Any]:
    """Force an immediate issue refresh (stubbed until Cluster 4)."""

    app, error = await _get_app_or_error()
    if error is not None:
        return _dump(error)
    return _dump(await control.issue_agent_fetch_now(app))


def main() -> None:
    """Console entrypoint — launches FastMCP on stdio."""

    mcp.run()


if __name__ == "__main__":
    main()
