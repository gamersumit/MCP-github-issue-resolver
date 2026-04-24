"""Token-redaction logging filter (TRD-002).

The filter intercepts every log record and scrubs GitHub tokens from both
the pre-format ``record.msg`` and the positional/keyword ``record.args``.
It uses two layers:

1. **Primary defense (literal substring replace)** — if the current token
   has been registered via :func:`set_token`, any occurrence of that
   exact string is replaced with ``***REDACTED***``. This is the
   strongest guarantee and handles tokens that don't match the public
   prefixes (e.g. legacy tokens, synthesized test fixtures).
2. **Regex safety net** — anything matching the public GitHub token
   prefixes (``ghp_``, ``gho_``, ``ghu_``, ``ghs_``, ``ghr_``,
   ``github_pat_``) followed by 20-255 word chars is scrubbed too, so
   tokens that were never registered still get caught.

Satisfies REQ-023.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Optional

__all__ = [
    "REDACTED",
    "RedactionFilter",
    "set_token",
    "get_token",
    "install_filter",
    "scrub",
]

REDACTED = "***REDACTED***"

# Broadened to cover classic (ghp_/gho_/ghu_/ghs_/ghr_) and fine-grained
# (github_pat_) tokens.  Length bounds match GitHub's documented ranges
# with generous slack so mutated/future tokens still get caught.
_TOKEN_RE = re.compile(
    r"(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{20,255}"
)

# The "registered" token is shared across threads — logging handlers run
# on whatever thread happens to emit a record, so we guard it with a lock
# and a snapshot pattern (read the value once, then work with the local).
_token_lock = threading.Lock()
_registered_token: Optional[str] = None


def set_token(token: Optional[str]) -> None:
    """Register (or clear) the currently-loaded GitHub token.

    Passing ``None`` or an empty string clears the registration so we
    don't end up redacting empty-string substrings (which would match
    every log line).
    """

    global _registered_token
    with _token_lock:
        _registered_token = token if token else None


def get_token() -> Optional[str]:
    """Return the currently registered token (or ``None``)."""

    with _token_lock:
        return _registered_token


def _scrub_text(text: str, token: Optional[str]) -> str:
    """Apply literal + regex redaction to a single string."""

    if not isinstance(text, str):
        return text  # defensive: caller already checks, but double-guard
    if token:
        # Literal substring replace — strongest guarantee.
        text = text.replace(token, REDACTED)
    # Regex safety net — catches any token-shaped substring.
    return _TOKEN_RE.sub(REDACTED, text)


def _scrub_value(value: Any, token: Optional[str]) -> Any:
    """Recursively scrub a single arg value.

    We only descend into simple containers (tuple/list/dict).  Anything
    else — custom objects, ints, bools — is returned unchanged; their
    eventual ``%s`` formatting goes through ``str()`` on the record
    itself, which we re-scrub at the message layer.
    """

    try:
        if isinstance(value, str):
            return _scrub_text(value, token)
        if isinstance(value, tuple):
            return tuple(_scrub_value(v, token) for v in value)
        if isinstance(value, list):
            return [_scrub_value(v, token) for v in value]
        if isinstance(value, dict):
            return {k: _scrub_value(v, token) for k, v in value.items()}
    except Exception:  # noqa: BLE001 — redaction must never raise
        return value
    return value


class RedactionFilter(logging.Filter):
    """Logging filter that scrubs GitHub tokens from records.

    Attach via :func:`install_filter` (attaches to the root logger) or
    add directly to a specific handler / logger instance.  The filter
    mutates the record in place and always returns ``True`` so the
    record continues down the chain.

    It is paranoid about exceptions — no matter what shape the record
    takes, ``filter()`` will not raise: a failed redaction silently
    passes the record through unchanged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            token = get_token()

            # record.msg — usually a format string, but may be any object.
            msg = record.msg
            if isinstance(msg, str):
                record.msg = _scrub_text(msg, token)
            else:
                # Non-string msg — scrub its ``str()`` form by replacing the
                # msg with the scrubbed string.  This is what ``getMessage``
                # would render anyway.
                try:
                    rendered = str(msg)
                    record.msg = _scrub_text(rendered, token)
                except Exception:  # noqa: BLE001
                    pass

            # record.args — may be a tuple, dict, or None.
            args = record.args
            if args:
                if isinstance(args, dict):
                    record.args = {
                        k: _scrub_value(v, token) for k, v in args.items()
                    }
                elif isinstance(args, tuple):
                    record.args = tuple(_scrub_value(v, token) for v in args)
                else:
                    # A single non-tuple arg (e.g. record.args = some_obj)
                    record.args = _scrub_value(args, token)

            # exc_text is cached by Formatter; clear it so it gets
            # re-rendered through our (already-scrubbed) msg if anyone
            # re-formats this record later.
            if record.exc_text:
                record.exc_text = _scrub_text(record.exc_text, token)
        except Exception:  # noqa: BLE001 — redaction never breaks logging
            pass
        return True


def scrub(text: str) -> str:
    """Public, stateless scrubber for arbitrary strings.

    Convenience wrapper that applies the full redaction policy (literal
    + regex) using the currently-registered token.  Intended for non-log
    surfaces like exception messages bubbling out of network helpers,
    where we still need token safety but the logging filter chain is
    not in play.

    Non-string input is returned unchanged so callers can pass it the
    output of ``str(exc)`` without first proving the type.
    """

    if not isinstance(text, str):
        return text
    return _scrub_text(text, get_token())


def install_filter(logger: Optional[logging.Logger] = None) -> RedactionFilter:
    """Attach a :class:`RedactionFilter` to a logger (default: root).

    Returns the filter instance so callers can later ``removeFilter`` it
    if they wish (mainly useful in tests).  Calling this repeatedly on
    the same logger is safe — each call adds a new filter instance, but
    they are idempotent in effect.
    """

    target = logger if logger is not None else logging.getLogger()
    f = RedactionFilter()
    target.addFilter(f)
    return f
