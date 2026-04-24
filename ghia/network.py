"""Network / rate-limit helpers (TRD-031).

Centralizes two concerns that previously lived inline in
:mod:`ghia.integrations.github` and were destined to be re-implemented
in every module that touches the network:

1. **Rate-limit reset formatting** — turn an epoch into a stable
   "resets at <iso> (in <relative>)" string so user-facing messages
   read identically wherever they come from.
2. **Transport-error classification** — map low-level transport
   exceptions (``ConnectionError``, ``TimeoutError``, ``OSError``,
   plus the httpx variants when installed) onto the canonical
   :class:`~ghia.errors.ErrorCode` set without ever leaking a token
   in the resulting message.

Token safety: every classified message is run through
:func:`ghia.redaction.scrub` before it is returned, so even an
exception whose payload includes the live token (e.g. an httpx
``ConnectError`` carrying the URL) cannot leak secrets to a tool
response or a log line.

Satisfies REQ-024.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple, Union

from ghia.errors import ErrorCode
from ghia.redaction import scrub

__all__ = [
    "format_rate_limit_reset",
    "classify_network_error",
]


def _format_relative(seconds: int) -> str:
    """Render ``seconds`` as ``"Xh Ym Zs"`` dropping leading zero parts.

    We omit the leading hour / minute when they are zero so a 5-minute
    wait reads as ``"5m 12s"`` rather than ``"0h 5m 12s"``.  The seconds
    component is always present (including ``0s``) so the string is
    never empty for a positive duration — easier to grep, easier to
    read, and the trailing ``s`` keeps the unit unambiguous.
    """

    if seconds <= 0:
        return "0s"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    # Include minutes when hours were emitted so the string stays
    # contiguous (e.g. "1h 0m 5s" not "1h 5s") — but skip otherwise to
    # honor the "drop leading zero parts" rule.
    if minutes or hours:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def format_rate_limit_reset(
    reset_epoch: Union[int, float, None],
) -> str:
    """Return a human-readable description of a rate-limit reset time.

    ``reset_epoch`` is the unix-epoch timestamp GitHub returns in the
    ``X-RateLimit-Reset`` header.  Three cases:

    * ``None`` → ``"resets at unknown time"`` so callers can drop the
      result straight into a sentence without branching on missing data.
    * Past epoch → ``"already reset"``; we don't render a negative
      duration because it's confusing.
    * Future epoch → ``"resets at YYYY-MM-DDTHH:MM:SSZ (in <rel>)"``
      with both the absolute and relative components so the user can
      pick whichever is more actionable.

    The absolute timestamp is always UTC ISO-8601 with a trailing ``Z``
    so logs read identically regardless of the host's local tz.
    """

    if reset_epoch is None:
        return "resets at unknown time"

    try:
        reset_dt = datetime.fromtimestamp(float(reset_epoch), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        # Defensive: any pathological epoch (NaN, way out of range)
        # collapses to "unknown" rather than crashing the error path.
        return "resets at unknown time"

    now = datetime.now(tz=timezone.utc)
    delta = reset_dt - now
    if delta.total_seconds() <= 0:
        return "already reset"

    # Render the absolute time with a "Z" suffix instead of "+00:00" so
    # the format matches what GitHub itself returns on most surfaces and
    # what humans expect when scanning a log line.
    iso = reset_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    rel = _format_relative(int(delta.total_seconds()))
    return f"resets at {iso} (in {rel})"


# --- Optional httpx import ------------------------------------------------
#
# httpx is a runtime dep (FastMCP pulls it in), but we import it lazily
# so this module remains importable in any future environment that
# trims it.  The module-level None sentinels keep the classification
# function's isinstance() chain cheap and branch-free.

try:  # pragma: no cover — exercised by environment, not test logic
    import httpx as _httpx

    _HTTPX_CONNECT_ERROR: Optional[type] = _httpx.ConnectError
    _HTTPX_TIMEOUT_ERROR: Optional[type] = _httpx.TimeoutException
except Exception:  # noqa: BLE001 — any import failure → treat as absent
    _HTTPX_CONNECT_ERROR = None
    _HTTPX_TIMEOUT_ERROR = None


def classify_network_error(exc: BaseException) -> Tuple[ErrorCode, str]:
    """Map a transport-layer exception to ``(ErrorCode, message)``.

    The mapping is deliberately conservative:

    * Known timeout shapes → ``NETWORK_ERROR`` with a "timeout" hint.
    * Known connection-refused / DNS shapes → ``NETWORK_ERROR``.
    * Generic ``OSError`` (catch-all for socket failures) →
      ``NETWORK_ERROR``.
    * Unrecognized exception → fall through to a "network error: <type>"
      message, still under ``NETWORK_ERROR``, so callers can rely on a
      single classification for retry / pause purposes.

    We never return ``RATE_LIMITED`` from a raw transport exception:
    rate-limit information lives in HTTP response headers, not in
    transport errors.  A future caller that already has a parsed 429
    response should construct the response code directly via
    :class:`ghia.errors.ErrorCode.RATE_LIMITED`; this helper exists for
    the layer below that.

    Token safety: the returned message is always passed through
    :func:`ghia.redaction.scrub`, so even an exception whose ``str()``
    happens to include the GitHub token (httpx exceptions can echo the
    request URL) cannot leak it through a tool response.
    """

    # Order matters: httpx.TimeoutException is a subclass of OSError on
    # some versions, so check the most specific type first.
    if _HTTPX_TIMEOUT_ERROR is not None and isinstance(
        exc, _HTTPX_TIMEOUT_ERROR
    ):
        message = f"network timeout: {type(exc).__name__}"
    elif _HTTPX_CONNECT_ERROR is not None and isinstance(
        exc, _HTTPX_CONNECT_ERROR
    ):
        message = f"could not connect: {type(exc).__name__}"
    elif isinstance(exc, TimeoutError):
        message = f"network timeout: {type(exc).__name__}"
    elif isinstance(exc, ConnectionError):
        message = f"could not connect: {type(exc).__name__}"
    elif isinstance(exc, OSError):
        # Catch-all for socket-level failures we didn't otherwise
        # name.  Including the errno hint (when present) gives ops
        # something to grep without pulling in the full traceback.
        errno_hint = f" (errno={exc.errno})" if getattr(exc, "errno", None) else ""
        message = f"network error: {type(exc).__name__}{errno_hint}"
    else:
        message = f"network error: {type(exc).__name__}"

    # If the exception itself carried a useful note (URL, hostname),
    # append it — but only after scrubbing.  We use ``str(exc)`` as a
    # *hint* not a *truth*: many exceptions render to an empty string,
    # in which case we keep the bare message.
    detail = str(exc).strip()
    if detail:
        message = f"{message}: {detail}"

    return ErrorCode.NETWORK_ERROR, scrub(message)
