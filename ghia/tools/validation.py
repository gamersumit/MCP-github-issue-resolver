"""Command allow-list validation for test/lint runners (TRD-008).

We accept user-supplied test/lint commands from two places: the setup
wizard (TRD-010) and — eventually — a future ``configure`` MCP tool.
In both cases an arbitrary shell string would be a RCE risk: a
malicious template could set ``test_command`` to ``"pytest; rm -rf ~"``
and the agent would dutifully execute it.

The allow-list here is the choke point.  The regex matches only the
binaries we explicitly support (all of which appear in the detection
rules in :mod:`ghia.detection`), followed by a bounded character set
that *excludes* shell metacharacters — no ``&``, ``|``, ``;``, ``$``,
backticks, redirects, or quoted strings are accepted.

Satisfies REQ-003 (AC-017-3).
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "COMMAND_ALLOW_RE",
    "InvalidCommandError",
    "validate_command",
]


# The allow-list.  Each alternative is a canonical runner / build-tool
# name anchored at the start of the string with ``^`` and followed by a
# word boundary so ``pytesting`` does NOT get past ``pytest``.  The tail
# pattern ``[\w\s\-=./,]*`` permits:
#   * word chars (flags, subcommand names)
#   * whitespace (argument separators)
#   * ``-`` (flag prefix)
#   * ``=`` (``--cov=ghia``)
#   * ``.`` (``ruff check .``, version numbers)
#   * ``/`` (paths: ``go test ./...``)
#   * ``,`` (CSV-style arg lists like ``rubocop -a,--parallel``)
# and deliberately excludes everything else — ``&``, ``|``, ``;``, ``$``,
# backticks, single/double quotes, redirection, globs.
COMMAND_ALLOW_RE: Final[re.Pattern[str]] = re.compile(
    r"^(pytest|python\s+-m\s+pytest|npm|npx|yarn|pnpm|jest|go|cargo|mvn|gradle|bundle|mix|"
    r"ruff|flake8|eslint|rubocop|golangci-lint|rake)\b[\w\s\-=./,]*$"
)


class InvalidCommandError(ValueError):
    """Raised when a command fails the allow-list.

    Inherits ``ValueError`` so call sites that already catch
    ``ValueError`` (e.g. Pydantic validators) keep working — but
    exposing a distinct type lets the wizard give a pointed error
    message.
    """


def validate_command(command: str, kind: str = "test") -> str:
    """Validate and return a trimmed command string.

    Args:
        command: The user-supplied command, e.g. ``"pytest -q"``.
            Leading / trailing whitespace is stripped; internal
            whitespace is preserved.
        kind: ``"test"`` or ``"lint"`` — used solely to craft the
            error message, not for validation logic.  Callers pass
            the appropriate label so the UI reads naturally.

    Returns:
        The stripped command.

    Raises:
        InvalidCommandError: if ``command`` doesn't match
            :data:`COMMAND_ALLOW_RE`.  The message names the ``kind``
            and gives the offending input so the user can spot the
            typo without us echoing it back mangled.
    """

    if not isinstance(command, str):
        raise InvalidCommandError(
            f"{kind} command must be a string, got {type(command).__name__}"
        )

    stripped = command.strip()
    if not stripped:
        raise InvalidCommandError(f"{kind} command is empty")

    if not COMMAND_ALLOW_RE.match(stripped):
        raise InvalidCommandError(
            f"{kind} command {stripped!r} is not on the allow-list. "
            f"Supported binaries: pytest, npm/npx/yarn/pnpm, jest, go, cargo, mvn, "
            f"gradle, bundle, mix, ruff, flake8, eslint, rubocop, golangci-lint, rake. "
            f"Shell metacharacters (& | ; $ ` ' \" > <) are not permitted."
        )

    return stripped
