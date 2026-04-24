"""Docker sandbox runner (TRD-026a / TRD-026b).

A thin wrapper around the ``docker`` SDK so the tool layer stays
testable.  No tool ever talks to the Docker daemon directly — they
go through :class:`DockerRunner`, which can be monkeypatched in
tests without touching the SDK or the network.

**Why a separate module:** TRD-026a/b draw a hard line between
"talk to Docker" and "decide what running tests means for this
issue".  Putting the SDK calls here means :mod:`ghia.tools.tests`
contains zero docker imports and can be unit-tested without a
running daemon (which doesn't exist in CI).

**Read-only repo mount:** ``/repo`` is mounted with
``read_only=True`` so a runaway test cannot scribble inside the
host's working tree.  Tests that genuinely need to write something
should target ``/tmp/test-output``, which is mounted read-write.

**Network:** kept at the daemon's default (allowed).  The TRD
doesn't require offline execution and many CI test suites (npm
test, mvn test, ...) need network access for their own caches.

**Timeout:** the caller passes ``timeout_sec``; we enforce it via
``container.wait(timeout=...)`` and on expiry return
``timed_out=True`` plus a non-zero ``exit_code``.  The container
is always force-removed in a ``finally`` block so a timeout doesn't
leak resources on the daemon.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "DockerRunner",
    "DockerUnavailable",
    "docker_available",
]


class DockerUnavailable(Exception):
    """Raised when the Docker daemon is missing or not reachable.

    Distinct from a generic exception so callers can branch
    ``except DockerUnavailable`` cleanly and map to
    :class:`ghia.errors.ErrorCode.DOCKER_UNAVAILABLE`.
    """


def docker_available() -> bool:
    """Return ``True`` iff the Docker daemon answers a ping.

    Used as an early-skip in :mod:`ghia.tools.tests` so we don't
    even attempt to mount volumes when Docker plainly isn't there.
    Catches both ``ImportError`` (the SDK isn't installed) and
    ``DockerException`` (the daemon socket is missing or the user
    lacks access) — either way the answer is "no".
    """

    try:
        import docker  # local import: SDK may not be installed
        from docker.errors import DockerException
    except ImportError:
        return False

    try:
        client = docker.from_env()
        client.ping()
        return True
    except DockerException:
        return False
    except Exception:  # noqa: BLE001 — defensive: SDK can throw odd errors
        # The docker SDK can raise transport-level exceptions
        # (urllib3, requests) that don't subclass DockerException.
        # We treat all of them as "no daemon".
        return False


class DockerRunner:
    """Run shell commands inside a one-shot container.

    One instance per call site is fine — the docker SDK manages its
    own connection pool internally and ``from_env()`` is cheap
    enough that we don't bother caching it on the runner.
    """

    def __init__(self, image: str = "python:3.11-slim") -> None:
        self.image = image
        # Lazy import: importing the SDK at module level would crash
        # the whole process on machines without docker-py installed,
        # which would defeat the entire docker_available() escape
        # hatch.  Resolved on every run_command call.
        self._docker_module: Any = None

    def _import_docker(self) -> Any:
        if self._docker_module is None:
            try:
                import docker  # local: SDK may not be installed
            except ImportError as exc:
                raise DockerUnavailable(
                    f"docker SDK not importable: {exc}"
                ) from exc
            self._docker_module = docker
        return self._docker_module

    async def run_command(
        self,
        *,
        repo_path: Path,
        command: list[str],
        timeout_sec: int = 600,
    ) -> dict[str, Any]:
        """Run ``command`` inside ``image``; return a structured result.

        Args:
            repo_path: Host path mounted read-only at ``/repo``.
            command: argv list executed inside the container (the
                container's entrypoint is preserved; we override it
                only via the explicit argv list).
            timeout_sec: Hard cap on the container's wall-clock
                runtime.  On expiry the container is killed and
                ``timed_out=True`` is reported.

        Returns:
            ``{exit_code, output, timed_out, duration_sec}``.
            ``output`` is combined stdout+stderr decoded with
            ``errors="replace"`` so binary noise doesn't crash the
            decode.

        Raises:
            DockerUnavailable: if the SDK isn't installed or the
                daemon is unreachable.  Other exceptions (e.g. an
                invalid image name surfacing as ``ImageNotFound``)
                are also wrapped into ``DockerUnavailable`` so the
                tool layer has exactly one failure mode to handle.
        """

        import asyncio  # local: keeps the module-level import surface tight

        docker = self._import_docker()
        from docker.errors import DockerException

        # ``docker.from_env`` reads DOCKER_HOST / DOCKER_TLS_VERIFY
        # etc., which is what users expect on a configured machine.
        try:
            client = docker.from_env()
        except DockerException as exc:
            raise DockerUnavailable(
                f"docker daemon unreachable: {exc}"
            ) from exc

        # Mount the repo read-only at /repo and a fresh tmpfs RW at
        # /tmp/test-output so legitimate test scratch space exists
        # without exposing the host filesystem.
        volumes = {
            str(repo_path.resolve()): {
                "bind": "/repo",
                "mode": "ro",
            },
        }
        # tmpfs for scratch — keyed by container path, value is a
        # mount-options string (empty string == default options).
        tmpfs = {"/tmp/test-output": ""}

        start = time.monotonic()
        container: Any = None
        timed_out = False
        exit_code = -1
        output = ""
        try:
            try:
                container = await asyncio.to_thread(
                    client.containers.run,
                    self.image,
                    command,
                    detach=True,
                    working_dir="/repo",
                    volumes=volumes,
                    tmpfs=tmpfs,
                    # read_only=False on the container's own
                    # filesystem because most languages need to
                    # write to /tmp during test runs; the bind
                    # mount we care about (``/repo``) is the one
                    # carrying ``mode=ro`` above.
                )
            except DockerException as exc:
                raise DockerUnavailable(
                    f"docker run failed: {exc}"
                ) from exc

            try:
                wait_result = await asyncio.to_thread(
                    container.wait, timeout=timeout_sec
                )
                exit_code = int(
                    wait_result.get("StatusCode", -1)
                    if isinstance(wait_result, dict)
                    else -1
                )
            except Exception as exc:  # noqa: BLE001 — timeout class varies by SDK transport
                # Both ``requests.exceptions.ReadTimeout`` and
                # ``docker.errors.APIError`` can surface here on a
                # wait timeout; treat any wait-side failure as a
                # timeout and proceed to log capture so the caller
                # at least sees what the container managed to print.
                logger.warning(
                    "container wait raised %s: %s — treating as timeout",
                    type(exc).__name__,
                    exc,
                )
                timed_out = True
                # Best-effort kill so we don't leak a running
                # container after we've decided the run is done.
                try:
                    await asyncio.to_thread(container.kill)
                except Exception:  # noqa: BLE001 — kill is best-effort
                    pass

            try:
                logs = await asyncio.to_thread(
                    container.logs, stdout=True, stderr=True
                )
                if isinstance(logs, bytes):
                    output = logs.decode("utf-8", errors="replace")
                else:
                    output = str(logs)
            except DockerException as exc:
                logger.warning("could not capture container logs: %s", exc)
                output = ""
        finally:
            # Always remove — keeps the daemon clean even on the
            # error path.  ``force=True`` covers the case where the
            # container is still running (e.g. wait timed out).
            if container is not None:
                try:
                    await asyncio.to_thread(container.remove, force=True)
                except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                    logger.warning("could not remove container: %s", exc)

        duration = time.monotonic() - start
        return {
            "exit_code": exit_code,
            "output": output,
            "timed_out": timed_out,
            "duration_sec": round(duration, 3),
        }
