"""TRD-002-TEST — verify token redaction utility.

Exercises BOTH classic PAT (``ghp_...``) and fine-grained PAT
(``github_pat_...``) redaction paths, plus the literal-registration
path and the regex safety-net path.
"""

from __future__ import annotations

import logging

import pytest

from ghia import redaction
from ghia.redaction import REDACTED, RedactionFilter, set_token


CLASSIC_PAT = "ghp_" + "A" * 36  # 40 chars total, well within 20-255 tail
FINE_GRAINED_PAT = (
    "github_pat_11ABCDEFG0" + "abcdefghij" * 6  # 82 chars total
)


@pytest.fixture(autouse=True)
def _reset_token() -> None:
    """Ensure every test starts with no registered token."""
    set_token(None)
    yield
    set_token(None)


def test_classic_pat_redacted_via_regex(captured_logger) -> None:
    log = logging.getLogger("ghia.test.classic")
    log.warning("oops token=%s", CLASSIC_PAT)

    assert captured_logger.messages, "handler captured nothing"
    msg = captured_logger.messages[0]
    assert CLASSIC_PAT not in msg
    assert REDACTED in msg


def test_fine_grained_pat_redacted_via_regex(captured_logger) -> None:
    """Critical test: fine-grained PATs (the reason we broadened the regex)."""
    log = logging.getLogger("ghia.test.fine")
    log.error("request failed with token %s", FINE_GRAINED_PAT)

    assert captured_logger.messages
    msg = captured_logger.messages[0]
    assert FINE_GRAINED_PAT not in msg
    assert REDACTED in msg


def test_literal_registration_redacts_arbitrary_string(captured_logger) -> None:
    """When a token is registered, its literal bytes are scrubbed.

    This covers tokens that don't match the public-prefix regex — e.g.,
    synthesized test tokens or any future prefix GitHub hasn't shipped.
    """
    weird_token = "x" * 30  # not a ghp_/github_pat_ token
    set_token(weird_token)

    log = logging.getLogger("ghia.test.literal")
    log.warning("raw: %s", weird_token)

    msg = captured_logger.messages[0]
    assert weird_token not in msg
    assert REDACTED in msg


def test_fine_grained_redacted_in_exception_text(captured_logger) -> None:
    """AC-023-4: fine-grained PAT in exception message must be scrubbed."""
    log = logging.getLogger("ghia.test.exc")
    try:
        raise RuntimeError(f"boom with token {FINE_GRAINED_PAT}")
    except RuntimeError:
        log.exception("caught")

    record = captured_logger.records[0]
    # The formatter has rendered the traceback into the handler output.
    # Ensure both the message and the eventual text don't leak the PAT.
    formatted = captured_logger.messages[0]
    assert FINE_GRAINED_PAT not in formatted
    # record.exc_text was populated post-filter; our filter re-scrubs it.
    if record.exc_text:
        assert FINE_GRAINED_PAT not in record.exc_text


def test_non_token_input_passes_through(captured_logger) -> None:
    log = logging.getLogger("ghia.test.clean")
    log.info("hello %s", "world")

    assert captured_logger.messages[0].endswith("hello world")


def test_short_prefix_not_redacted(captured_logger) -> None:
    """Regex requires at least 20 chars after the prefix."""
    log = logging.getLogger("ghia.test.short")
    too_short = "ghp_abc123"  # 10 chars total
    log.info("this is not a token: %s", too_short)

    assert too_short in captured_logger.messages[0]


def test_set_token_empty_string_clears(captured_logger) -> None:
    """Registering '' must NOT cause runaway replacement of every empty gap."""
    set_token("")
    # Empty-string registration should behave like None.
    assert redaction.get_token() is None

    log = logging.getLogger("ghia.test.empty")
    log.info("normal message with spaces")
    msg = captured_logger.messages[0]
    # No REDACTED should appear just because we registered ''.
    assert REDACTED not in msg


def test_filter_tolerates_malformed_record() -> None:
    """Redaction must never raise, even on odd record shapes."""
    f = RedactionFilter()

    class _Odd:
        def __str__(self) -> str:
            raise RuntimeError("str() blows up")

    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=_Odd(),
        args=None,
        exc_info=None,
    )
    assert f.filter(record) is True  # no raise


def test_install_filter_attaches_to_root() -> None:
    root = logging.getLogger()
    before = len(root.filters)
    f = redaction.install_filter()
    try:
        assert f in root.filters
        assert len(root.filters) == before + 1
    finally:
        root.removeFilter(f)
