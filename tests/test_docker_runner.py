"""Docker runner tests (TRD-026a/b-TEST — runner half).

We exercise :class:`DockerRunner` via mocks because CI doesn't have
a daemon.  The mocks check the exact kwargs we pass to
``client.containers.run`` — that's the contract that guarantees the
read-only mount and the tmpfs scratch dir.

Coverage:
* ``docker_available`` returns False when SDK import fails
* ``docker_available`` returns False when daemon is unreachable
* ``run_command`` mounts ``/repo`` read-only
* ``run_command`` mounts a tmpfs at ``/tmp/test-output``
* ``run_command`` returns ``{exit_code, output, ...}`` from the
  container outputs
* ``run_command`` always removes the container, even on the error
  path
* daemon-unreachable raises ``DockerUnavailable``
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from ghia.integrations import docker_runner as dr_mod
from ghia.integrations.docker_runner import (
    DockerRunner,
    DockerUnavailable,
    docker_available,
)


# ----------------------------------------------------------------------
# docker_available
# ----------------------------------------------------------------------


def test_docker_available_false_when_sdk_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Block the docker import and assert the function reports False."""

    real_import = __builtins__["__import__"] if isinstance(
        __builtins__, dict
    ) else __builtins__.__import__

    def _blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "docker" or name.startswith("docker."):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _blocked_import)
    assert docker_available() is False


def test_docker_available_false_when_daemon_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``from_env().ping()`` raises DockerException → returns False."""

    import docker
    from docker.errors import DockerException

    def _from_env_raises() -> Any:
        raise DockerException("no daemon")

    monkeypatch.setattr(docker, "from_env", _from_env_raises)
    assert docker_available() is False


# ----------------------------------------------------------------------
# DockerRunner.run_command — fakes
# ----------------------------------------------------------------------


class _FakeContainer:
    """Minimal stand-in for a ``docker.Container`` instance."""

    def __init__(
        self,
        *,
        wait_result: dict[str, int] | None = None,
        wait_raises: Exception | None = None,
        logs_bytes: bytes = b"",
    ) -> None:
        self.wait_result = wait_result or {"StatusCode": 0}
        self.wait_raises = wait_raises
        self.logs_bytes = logs_bytes
        self.killed = False
        self.removed = False

    def wait(self, timeout: int | None = None) -> dict[str, int]:
        if self.wait_raises is not None:
            raise self.wait_raises
        return self.wait_result

    def logs(self, stdout: bool = True, stderr: bool = True) -> bytes:
        return self.logs_bytes

    def kill(self) -> None:
        self.killed = True

    def remove(self, force: bool = False) -> None:
        self.removed = True


class _FakeContainersAPI:
    """Stand-in for ``client.containers``."""

    def __init__(self, container: _FakeContainer) -> None:
        self.container = container
        self.run_kwargs: dict[str, Any] = {}
        self.run_args: tuple[Any, ...] = ()

    def run(self, *args: Any, **kwargs: Any) -> _FakeContainer:
        self.run_args = args
        self.run_kwargs = kwargs
        return self.container


class _FakeClient:
    def __init__(self, container: _FakeContainer) -> None:
        self.containers = _FakeContainersAPI(container)


def _patch_docker_module(
    monkeypatch: pytest.MonkeyPatch, container: _FakeContainer
) -> _FakeClient:
    """Inject a fake docker module so DockerRunner uses it."""

    fake_client = _FakeClient(container)

    # Build a minimal ``docker`` module shape — just enough for the
    # runner's lazy import.  We also need ``docker.errors`` because
    # the runner imports ``DockerException`` from there.
    import docker as real_docker  # noqa: F401 — ensure real package available
    fake_module = types.SimpleNamespace(
        from_env=lambda: fake_client,
        # Keep the real errors module so the ``except`` clauses still
        # match by class identity.
        errors=real_docker.errors,
    )
    monkeypatch.setitem(sys.modules, "docker", fake_module)
    return fake_client


# ----------------------------------------------------------------------
# run_command happy path + mount checks
# ----------------------------------------------------------------------


async def test_run_command_mounts_repo_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = _FakeContainer(logs_bytes=b"hello\n")
    fake_client = _patch_docker_module(monkeypatch, container)

    runner = DockerRunner()
    await runner.run_command(
        repo_path=tmp_path,
        command=["sh", "-c", "echo hello"],
        timeout_sec=60,
    )

    volumes = fake_client.containers.run_kwargs.get("volumes")
    assert volumes is not None, "runner must pass a volumes kwarg"
    # The host path should map to /repo with mode=ro.
    repo_entry = volumes[str(tmp_path.resolve())]
    assert repo_entry["bind"] == "/repo"
    assert repo_entry["mode"] == "ro"


async def test_run_command_provides_tmpfs_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = _FakeContainer()
    fake_client = _patch_docker_module(monkeypatch, container)

    runner = DockerRunner()
    await runner.run_command(
        repo_path=tmp_path,
        command=["sh", "-c", "true"],
        timeout_sec=10,
    )

    tmpfs = fake_client.containers.run_kwargs.get("tmpfs")
    assert tmpfs is not None
    assert "/tmp/test-output" in tmpfs


async def test_run_command_returns_structured_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = _FakeContainer(
        wait_result={"StatusCode": 0},
        logs_bytes=b"all green\n",
    )
    _patch_docker_module(monkeypatch, container)

    runner = DockerRunner()
    result = await runner.run_command(
        repo_path=tmp_path,
        command=["sh", "-c", "true"],
        timeout_sec=10,
    )

    assert result["exit_code"] == 0
    assert "all green" in result["output"]
    assert result["timed_out"] is False
    assert isinstance(result["duration_sec"], float)


async def test_run_command_propagates_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = _FakeContainer(
        wait_result={"StatusCode": 1},
        logs_bytes=b"failed!\n",
    )
    _patch_docker_module(monkeypatch, container)

    result = await DockerRunner().run_command(
        repo_path=tmp_path,
        command=["sh", "-c", "false"],
        timeout_sec=10,
    )
    assert result["exit_code"] == 1
    assert result["timed_out"] is False
    assert "failed" in result["output"]


async def test_run_command_marks_timed_out_on_wait_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = _FakeContainer(wait_raises=TimeoutError("read timeout"))
    _patch_docker_module(monkeypatch, container)

    result = await DockerRunner().run_command(
        repo_path=tmp_path,
        command=["sh", "-c", "sleep 1000"],
        timeout_sec=1,
    )
    assert result["timed_out"] is True
    assert container.killed is True


async def test_run_command_always_removes_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = _FakeContainer(wait_raises=TimeoutError("x"))
    _patch_docker_module(monkeypatch, container)

    await DockerRunner().run_command(
        repo_path=tmp_path,
        command=["sh", "-c", "x"],
        timeout_sec=1,
    )
    assert container.removed is True


# ----------------------------------------------------------------------
# Daemon-unreachable error path
# ----------------------------------------------------------------------


async def test_run_command_raises_docker_unavailable_when_daemon_dies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import docker as real_docker
    from docker.errors import DockerException

    def _bad_from_env() -> Any:
        raise DockerException("daemon down")

    fake_module = types.SimpleNamespace(
        from_env=_bad_from_env,
        errors=real_docker.errors,
    )
    monkeypatch.setitem(sys.modules, "docker", fake_module)

    with pytest.raises(DockerUnavailable):
        await DockerRunner().run_command(
            repo_path=tmp_path,
            command=["sh", "-c", "true"],
            timeout_sec=5,
        )
