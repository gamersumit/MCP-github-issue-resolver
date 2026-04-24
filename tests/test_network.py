"""Tests for ghia.network — rate-limit formatting + transport classifier (TRD-031-TEST)."""

from __future__ import annotations

import re
import time

import pytest

from ghia import redaction
from ghia.errors import ErrorCode
from ghia.network import classify_network_error, format_rate_limit_reset


# ----------------------------------------------------------------------
# format_rate_limit_reset
# ----------------------------------------------------------------------


def test_format_reset_none_returns_unknown() -> None:
    assert format_rate_limit_reset(None) == "resets at unknown time"


def test_format_reset_past_returns_already_reset() -> None:
    # 10 seconds ago — no chance of a clock-skew flake here.
    past_epoch = int(time.time()) - 10
    assert format_rate_limit_reset(past_epoch) == "already reset"


def test_format_reset_future_returns_iso_and_relative() -> None:
    # ~5 minutes 12 seconds in the future.
    future_epoch = int(time.time()) + 312
    out = format_rate_limit_reset(future_epoch)

    # Absolute portion: ISO-8601 with trailing Z.
    iso_match = re.search(r"resets at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", out)
    assert iso_match, out

    # Relative portion: "Xm Ys" or similar — leading zero hour must be dropped.
    assert "(in " in out
    rel = re.search(r"\(in ([^)]+)\)", out)
    assert rel, out
    rel_text = rel.group(1)
    # No leading "0h" — the helper drops it explicitly.
    assert not rel_text.startswith("0h")
    # Should be in the "Xm Ys" shape (allow off-by-one second slack).
    assert "m" in rel_text and "s" in rel_text


def test_format_reset_long_future_includes_hours() -> None:
    # ~1h 30m in the future — hours bucket should be present.
    future_epoch = int(time.time()) + 3600 + 1800 + 5
    out = format_rate_limit_reset(future_epoch)
    rel = re.search(r"\(in ([^)]+)\)", out)
    assert rel, out
    rel_text = rel.group(1)
    assert "h" in rel_text and "m" in rel_text and "s" in rel_text


def test_format_reset_invalid_epoch_falls_back_to_unknown() -> None:
    """A garbage epoch must collapse to 'unknown', not raise."""

    # OverflowError or OSError is what fromtimestamp raises for huge values.
    assert format_rate_limit_reset(1e20) == "resets at unknown time"


# ----------------------------------------------------------------------
# classify_network_error
# ----------------------------------------------------------------------


def test_classify_connection_error() -> None:
    code, msg = classify_network_error(ConnectionError("boom"))
    assert code is ErrorCode.NETWORK_ERROR
    assert "could not connect" in msg
    assert "ConnectionError" in msg


def test_classify_timeout_error() -> None:
    code, msg = classify_network_error(TimeoutError("slow"))
    assert code is ErrorCode.NETWORK_ERROR
    assert "timeout" in msg.lower()


def test_classify_generic_oserror() -> None:
    exc = OSError(54, "some socket failure")
    code, msg = classify_network_error(exc)
    assert code is ErrorCode.NETWORK_ERROR
    assert "network error" in msg
    # Errno hint is included when available.
    assert "errno=54" in msg


def test_classify_unknown_exception_defaults_to_network_error() -> None:
    class _Weird(Exception):
        pass

    code, msg = classify_network_error(_Weird("nope"))
    assert code is ErrorCode.NETWORK_ERROR
    assert "_Weird" in msg


def test_classify_httpx_connect_error() -> None:
    httpx = pytest.importorskip("httpx")

    code, msg = classify_network_error(
        httpx.ConnectError("dns lookup failed for api.github.com")
    )
    assert code is ErrorCode.NETWORK_ERROR
    assert "could not connect" in msg


def test_classify_httpx_timeout_error() -> None:
    httpx = pytest.importorskip("httpx")

    code, msg = classify_network_error(httpx.ReadTimeout("read timed out"))
    assert code is ErrorCode.NETWORK_ERROR
    assert "timeout" in msg.lower()


def test_classify_strips_registered_token_from_message() -> None:
    """A token leaking through exception text must NEVER reach the response."""

    fake_token = "ghp_" + "Z" * 36
    redaction.set_token(fake_token)
    try:
        # Synthesize an exception whose str() carries the token, mimicking
        # what httpx sometimes does when an URL with embedded credentials
        # ends up in the error message.
        exc = ConnectionError(f"could not reach https://x-access-token:{fake_token}@api.github.com")
        code, msg = classify_network_error(exc)
        assert code is ErrorCode.NETWORK_ERROR
        assert fake_token not in msg
        assert "REDACTED" in msg
    finally:
        redaction.set_token(None)


def test_classify_strips_token_shaped_substring_even_without_register() -> None:
    """Regex safety net catches a token-shaped string we never registered."""

    redaction.set_token(None)
    leaked = "ghp_" + "A" * 36  # token shape, but never registered
    exc = ConnectionError(f"failed for header={leaked}")
    code, msg = classify_network_error(exc)
    assert code is ErrorCode.NETWORK_ERROR
    assert leaked not in msg
    assert "REDACTED" in msg
