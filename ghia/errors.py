"""Structured error types and response schema for MCP tools (TRD-001).

Every MCP tool returns a :class:`ToolResponse` — never a raw exception or
stack trace. This module defines the canonical error-code enum, the
response schema (Pydantic v2), ergonomic helpers, and the ``wrap_tool``
decorator that converts stray exceptions into structured responses.

Satisfies REQ-025.
"""

from __future__ import annotations

import functools
import inspect
import logging
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, model_validator

logger = logging.getLogger(__name__)

__all__ = [
    "ErrorCode",
    "ToolResponse",
    "ok",
    "err",
    "wrap_tool",
]


class ErrorCode(str, Enum):
    """Canonical error codes for every tool response.

    The set is closed: tools may only emit codes defined here.  This keeps
    error reporting auditable and lets callers switch on stable strings.
    """

    TOKEN_INVALID = "TOKEN_INVALID"
    REPO_NOT_FOUND = "REPO_NOT_FOUND"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    TEST_FAILED = "TEST_FAILED"
    DOCKER_UNAVAILABLE = "DOCKER_UNAVAILABLE"
    GIT_ERROR = "GIT_ERROR"
    GIT_NOT_FOUND = "GIT_NOT_FOUND"
    NETWORK_ERROR = "NETWORK_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    BRANCH_EXISTS = "BRANCH_EXISTS"
    PR_EXISTS = "PR_EXISTS"
    UNDO_REFUSED_NOT_OURS = "UNDO_REFUSED_NOT_OURS"
    UNDO_REFUSED_PROTECTED_BRANCH = "UNDO_REFUSED_PROTECTED_BRANCH"
    CONFIG_MISSING = "CONFIG_MISSING"
    INVALID_INPUT = "INVALID_INPUT"
    ON_DEFAULT_BRANCH_REFUSED = "ON_DEFAULT_BRANCH_REFUSED"
    NO_DEFAULT_BRANCH_DETECTED = "NO_DEFAULT_BRANCH_DETECTED"


class ToolResponse(BaseModel):
    """Canonical response envelope returned by every MCP tool.

    Invariants (enforced by a ``model_validator``):

    * ``success == True``  → ``data`` MUST be present; ``error`` and
      ``code`` MUST be ``None``.
    * ``success == False`` → ``error`` and ``code`` MUST be present;
      ``data`` MUST be ``None``.

    Note that a successful payload of ``None`` is still allowed — we just
    require the ``data`` field to be explicitly set (we cannot distinguish
    "unset" from "``None``" on a Pydantic model without extra plumbing).
    Callers that want a no-payload success should pass ``ok(None)``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    code: Optional[ErrorCode] = None

    @model_validator(mode="after")
    def _check_shape(self) -> "ToolResponse":
        if self.success:
            if self.error is not None or self.code is not None:
                raise ValueError(
                    "ToolResponse(success=True) must have error=None and code=None"
                )
        else:
            if self.error is None or self.code is None:
                raise ValueError(
                    "ToolResponse(success=False) must have both error and code set"
                )
            if self.data is not None:
                raise ValueError(
                    "ToolResponse(success=False) must have data=None"
                )
        return self


def ok(data: Any = None) -> ToolResponse:
    """Build a successful :class:`ToolResponse` carrying ``data``."""

    return ToolResponse(success=True, data=data, error=None, code=None)


def err(code: ErrorCode, msg: str) -> ToolResponse:
    """Build a failed :class:`ToolResponse` with ``code`` and ``msg``."""

    return ToolResponse(success=False, data=None, error=msg, code=code)


F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def wrap_tool(func: F) -> F:
    """Decorator that converts any exception to a structured error response.

    Wraps an async function so that raw exceptions never reach the MCP
    transport — the client always sees ``{success: false, error, code}``
    rather than a stack trace. If the wrapped function already returns a
    :class:`ToolResponse`, it is passed through unchanged.

    Only supports ``async`` callables; raises ``TypeError`` at decoration
    time otherwise, so misuse fails fast during import.
    """

    if not inspect.iscoroutinefunction(func):
        raise TypeError(
            f"wrap_tool requires an async function; got {func!r}"
        )

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> ToolResponse:
        try:
            result = await func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — we intentionally catch everything
            logger.warning(
                "tool %s raised %s: %s",
                func.__qualname__,
                type(exc).__name__,
                exc,
            )
            return err(ErrorCode.INVALID_INPUT, str(exc))
        if isinstance(result, ToolResponse):
            return result
        # Tool returned a bare value — wrap it in a success envelope.
        return ok(result)

    return wrapper  # type: ignore[return-value]
