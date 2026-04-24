"""TRD-001-TEST — verify error types + structured response schema.

Validates PRD AC-025-1 (any tool exception becomes a structured error),
AC-025-2 (only the canonical enum codes are usable).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ghia.errors import ErrorCode, ToolResponse, err, ok, wrap_tool


CANONICAL_CODES = {
    "TOKEN_INVALID",
    "REPO_NOT_FOUND",
    "FILE_NOT_FOUND",
    "PATH_TRAVERSAL",
    "TEST_FAILED",
    "DOCKER_UNAVAILABLE",
    "GIT_ERROR",
    "GIT_NOT_FOUND",
    "NETWORK_ERROR",
    "RATE_LIMITED",
    "BRANCH_EXISTS",
    "PR_EXISTS",
    "UNDO_REFUSED_NOT_OURS",
    "UNDO_REFUSED_PROTECTED_BRANCH",
    "CONFIG_MISSING",
    "INVALID_INPUT",
    "ON_DEFAULT_BRANCH_REFUSED",
    "NO_DEFAULT_BRANCH_DETECTED",
}


def test_error_enum_is_closed_set() -> None:
    """Exactly the canonical codes exist — no extras, no missing."""
    members = {m.value for m in ErrorCode}
    assert members == CANONICAL_CODES


def test_ok_builds_success_response() -> None:
    resp = ok({"hello": "world"})
    assert resp.success is True
    assert resp.data == {"hello": "world"}
    assert resp.error is None
    assert resp.code is None


def test_ok_allows_none_payload() -> None:
    resp = ok(None)
    assert resp.success is True
    assert resp.data is None


def test_err_builds_failure_response() -> None:
    resp = err(ErrorCode.REPO_NOT_FOUND, "nope")
    assert resp.success is False
    assert resp.error == "nope"
    assert resp.code is ErrorCode.REPO_NOT_FOUND
    assert resp.data is None


def test_success_with_error_rejected() -> None:
    with pytest.raises(ValidationError):
        ToolResponse(success=True, error="uh oh", code=None)


def test_failure_without_code_rejected() -> None:
    with pytest.raises(ValidationError):
        ToolResponse(success=False, error="broke", code=None)


def test_failure_with_data_rejected() -> None:
    with pytest.raises(ValidationError):
        ToolResponse(
            success=False,
            data={"x": 1},
            error="conflict",
            code=ErrorCode.INVALID_INPUT,
        )


async def test_wrap_tool_catches_exceptions() -> None:
    @wrap_tool
    async def boom() -> None:
        raise RuntimeError("kaboom")

    resp = await boom()
    assert resp.success is False
    assert resp.code is ErrorCode.INVALID_INPUT
    assert "kaboom" in resp.error


async def test_wrap_tool_passes_through_tool_response() -> None:
    @wrap_tool
    async def happy() -> ToolResponse:
        return ok({"x": 1})

    resp = await happy()
    assert resp.success is True
    assert resp.data == {"x": 1}


async def test_wrap_tool_wraps_bare_return() -> None:
    @wrap_tool
    async def bare() -> dict:
        return {"answer": 42}

    resp = await bare()
    assert resp.success is True
    assert resp.data == {"answer": 42}


def test_wrap_tool_rejects_sync_callable() -> None:
    with pytest.raises(TypeError):

        @wrap_tool  # type: ignore[arg-type]
        def not_async() -> int:  # pragma: no cover — decoration raises
            return 1
