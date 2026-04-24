"""Naming helper tests (TRD-032-TEST).

Covers:
* slugify: lowercase, special-char collapsing, length cap, empty fallback,
  trailing-dash strip after truncation
* branch_name / commit_msg / pr_title format spot-checks
* Consistency: all four helpers exercised against the same (n, title) input
"""

from __future__ import annotations

import pytest

from ghia.naming import branch_name, commit_msg, pr_title, slugify


# ----------------------------------------------------------------------
# slugify
# ----------------------------------------------------------------------


def test_slugify_lowercases() -> None:
    assert slugify("Hello World") == "hello-world"


def test_slugify_collapses_special_chars() -> None:
    # !!!, spaces, mixed punctuation all collapse to single dashes.
    assert slugify("Foo!!! @bar  $$$baz") == "foo-bar-baz"


def test_slugify_strips_leading_trailing_dashes() -> None:
    assert slugify("---hello---") == "hello"
    assert slugify("...hello...") == "hello"


def test_slugify_collapses_repeated_dashes() -> None:
    assert slugify("foo----bar") == "foo-bar"


def test_slugify_caps_length() -> None:
    long_title = "a" * 100
    out = slugify(long_title, max_len=40)
    assert len(out) == 40
    assert out == "a" * 40


def test_slugify_strips_trailing_dash_after_truncation() -> None:
    """Truncation that lands on a dash must not leave one trailing.

    "fix-the-bug-now" truncated to 8 chars is "fix-the-" which we
    don't want — the helper must re-strip to "fix-the".
    """

    out = slugify("fix the bug now", max_len=8)
    assert not out.endswith("-")
    assert out == "fix-the"


def test_slugify_empty_returns_fallback() -> None:
    assert slugify("") == "issue"
    assert slugify("!!!") == "issue"
    assert slugify("   ") == "issue"


def test_slugify_unicode_falls_back_to_dashes() -> None:
    """Non-ASCII letters get replaced; if nothing's left, we get the fallback."""

    # café -> "caf-" -> "caf" after trailing-dash strip.
    assert slugify("café") == "caf"
    # All non-ASCII -> fallback.
    assert slugify("日本語") == "issue"


def test_slugify_handles_numbers() -> None:
    assert slugify("Issue 123 fix") == "issue-123-fix"


def test_slugify_default_max_len_is_40() -> None:
    out = slugify("x" * 50)
    assert len(out) == 40


def test_slugify_custom_max_len_zero_or_negative_returns_fallback() -> None:
    # max_len=0 truncates to empty -> fallback.
    assert slugify("hello", max_len=0) == "issue"


def test_slugify_non_string_input_coerces() -> None:
    # Defensive: a stray None shouldn't crash.
    assert slugify(None) == "issue"  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# branch_name
# ----------------------------------------------------------------------


def test_branch_name_basic() -> None:
    assert branch_name(42, "Fix the bug") == "fix-issue-42-fix-the-bug"


def test_branch_name_with_special_chars_in_title() -> None:
    assert branch_name(1, "Foo!!! @bar") == "fix-issue-1-foo-bar"


def test_branch_name_empty_title_uses_fallback() -> None:
    assert branch_name(7, "") == "fix-issue-7-issue"


def test_branch_name_long_title_truncates() -> None:
    # Slug is capped at 40 chars; prefix is unbounded.
    title = "a" * 100
    out = branch_name(99, title)
    assert out == f"fix-issue-99-{'a' * 40}"


# ----------------------------------------------------------------------
# commit_msg
# ----------------------------------------------------------------------


def test_commit_msg_basic_preserves_original_casing() -> None:
    assert commit_msg(42, "Fix THE Bug") == "fix(#42): Fix THE Bug"


def test_commit_msg_strips_outer_whitespace() -> None:
    assert commit_msg(1, "  hello  ") == "fix(#1): hello"


def test_commit_msg_empty_title_omits_colon() -> None:
    assert commit_msg(5, "") == "fix(#5)"
    assert commit_msg(5, "   ") == "fix(#5)"


def test_commit_msg_special_chars_preserved_verbatim() -> None:
    """Commit messages keep punctuation exactly — readers want fidelity."""

    assert commit_msg(7, "Crash: NullPointer in foo()") == \
        "fix(#7): Crash: NullPointer in foo()"


# ----------------------------------------------------------------------
# pr_title
# ----------------------------------------------------------------------


def test_pr_title_basic() -> None:
    assert pr_title(42, "Fix THE Bug") == "Fix #42: Fix THE Bug"


def test_pr_title_strips_outer_whitespace() -> None:
    assert pr_title(1, "  hello  ") == "Fix #1: hello"


def test_pr_title_empty_title_omits_colon() -> None:
    assert pr_title(5, "") == "Fix #5"
    assert pr_title(5, "   ") == "Fix #5"


# ----------------------------------------------------------------------
# Consistency across all four helpers (TRD-032-TEST AC)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "number, title",
    [
        (1, "Hello world"),
        (42, "Fix the THING!!!"),
        (999, "  trim me  "),
        (5, ""),
    ],
)
def test_all_four_helpers_agree_on_issue_number(number: int, title: str) -> None:
    """branch_name, commit_msg, pr_title must all reference the same N."""

    bn = branch_name(number, title)
    cm = commit_msg(number, title)
    pt = pr_title(number, title)

    # branch_name is the slug form.
    assert bn.startswith(f"fix-issue-{number}-")
    # commit_msg uses the conventional-commit prefix.
    assert cm.startswith(f"fix(#{number})")
    # pr_title uses the GitHub-friendly prefix.
    assert pt.startswith(f"Fix #{number}")
