"""Test-runner MCP tool (TRD-026a / TRD-026b).

One MCP tool — :func:`run_tests` — that executes the configured
``test_command`` inside a Docker sandbox via
:class:`ghia.integrations.docker_runner.DockerRunner`.

Why a sandbox: the tests are coming straight off a freshly-checked-out
remote and the agent might be running on a developer machine.  Running
arbitrary test commands on the host filesystem would be a security
disaster (think a malicious test that runs ``rm -rf $HOME`` in CI).
The sandbox guarantees:

* The repo is mounted **read-only** so a runaway test cannot mutate
  the working tree.
* The host user / network namespace is the daemon's, not the
  developer's home account.
* A 10-minute wall clock kills any test suite that hangs.

Result shape: success-with-passed=false on a normal test failure
(rather than ``TEST_FAILED``).  ``TEST_FAILED`` is reserved for
infrastructure failures the user can't fix from their code (Docker
exiting mid-run after passing the availability check).  This split
matters because callers care about the difference between "your tests
have bugs" and "the test platform is broken".

Satisfies REQ-017.
"""

from __future__ import annotations

import logging
from typing import Any

from ghia.app import GhiaApp
from ghia.errors import ErrorCode, ToolResponse, err, ok, wrap_tool
from ghia.integrations.docker_runner import (
    DockerRunner,
    DockerUnavailable,
    docker_available,
)
from ghia.tools.validation import InvalidCommandError, validate_command

logger = logging.getLogger(__name__)

__all__ = ["run_tests"]


# 10-minute hard cap per TRD-026a.  Long enough for big suites,
# short enough that a hanging test eventually frees the slot.
_TEST_TIMEOUT_S = 600


@wrap_tool
async def run_tests(app: GhiaApp) -> ToolResponse:
    """Run the configured test command inside a Docker sandbox.

    Behaviour matrix:

    * No ``test_command`` configured → ``ok({skipped: true, ...})``.
    * Command fails the allow-list (defense-in-depth) →
      ``err(INVALID_INPUT, ...)``.
    * Docker daemon unreachable → ``err(DOCKER_UNAVAILABLE, ...)``
      (no trace, just the structured code).
    * Docker mid-run failure (image pull dies, daemon vanishes) →
      ``err(TEST_FAILED, ...)`` — this is the only case where we use
      ``TEST_FAILED``.
    * Tests ran to completion → ``ok({passed, output, exit_code,
      timed_out, duration_sec})`` regardless of pass/fail.  Callers
      branch on ``data["passed"]``.
    """

    cmd_str = app.config.test_command
    if not cmd_str or not cmd_str.strip():
        return ok({"skipped": True, "reason": "no test command configured"})

    # Defense-in-depth: re-validate even though config-load already
    # checked.  An in-memory mutation should not bypass the
    # allow-list.
    try:
        validate_command(cmd_str, kind="test")
    except InvalidCommandError as exc:
        return err(ErrorCode.INVALID_INPUT, str(exc))

    if not docker_available():
        return err(
            ErrorCode.DOCKER_UNAVAILABLE,
            "Docker daemon not reachable",
        )

    # The allow-list already vetted the command, so wrapping it in
    # ``sh -c`` is purely an invocation convenience — we get pipes
    # and arg parsing inside the container without re-implementing
    # them here.  No shell-injection risk because cmd_str is a
    # single argv element with controlled contents.
    runner = DockerRunner()
    container_argv = ["sh", "-c", cmd_str]

    try:
        result = await runner.run_command(
            repo_path=app.repo_root,
            command=container_argv,
            timeout_sec=_TEST_TIMEOUT_S,
        )
    except DockerUnavailable as exc:
        # The availability check passed but the daemon vanished
        # mid-run (or the image pull failed).  This is genuine
        # infrastructure failure → TEST_FAILED.
        return err(ErrorCode.TEST_FAILED, f"docker run failed mid-flight: {exc}")

    payload: dict[str, Any] = {
        "passed": result["exit_code"] == 0 and not result["timed_out"],
        "output": result["output"],
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
        "duration_sec": result["duration_sec"],
    }
    return ok(payload)
