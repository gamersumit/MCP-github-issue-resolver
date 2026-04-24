"""TRD-034-TEST: README.md section + code-block audit."""

from __future__ import annotations

import re
from pathlib import Path

import pytest


# WHY: keep the path resolution local — README lives at the repo root, two
# levels up from this test file. Anchoring with __file__ keeps the test
# runnable from any cwd (matters for `pytest tests/test_readme.py`).
README_PATH = Path(__file__).resolve().parent.parent / "README.md"


REQUIRED_SECTIONS = [
    "Install",
    "Quick Start",
    "Commands",
    "Modes",
    "Token how-to",
    "Security",
    "Troubleshooting",
    # WHY: the PRD mandates 8 sections; the TRD-034 AC names 7 explicitly
    # ("Install, Quick Start, Commands, Modes, Token howto, Security,
    # Troubleshooting") and leaves the 8th as "the remaining PRD-mandated
    # section". Architecture is what the codebase + protocol actually
    # need readers to grok — chosen here.
    "Architecture",
]


# WHY: shell commands embedded in the README must reference real binaries
# the project actually uses. Anything outside this set is almost certainly
# a placeholder copied from a template (npm/yarn/ts-node/etc don't apply
# to this Python project) and the test should fail loudly.
ALLOWED_COMMANDS = {
    "git",
    "bash",
    "python",
    "python3",
    "pip",
    "gh",
    "claude",
    "docker",
    "source",
    "cd",
    "mkdir",
    "chmod",
    "sudo",  # appears in troubleshooting hints (sudo systemctl start docker)
    "brew",  # platform install hint (brew install git)
}


def _read_readme() -> str:
    return README_PATH.read_text(encoding="utf-8")


def test_readme_exists() -> None:
    assert README_PATH.is_file(), f"README missing at {README_PATH}"


def test_readme_has_required_sections() -> None:
    text = _read_readme()
    # H2 only — H1 is the project title, H3 are sub-sections.
    h2_pattern = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
    found = {m.group(1).strip() for m in h2_pattern.finditer(text)}

    missing = [s for s in REQUIRED_SECTIONS if s not in found]
    assert not missing, (
        f"README missing required H2 sections: {missing}. "
        f"Found H2s: {sorted(found)}"
    )


def _extract_shell_blocks(text: str) -> list[str]:
    # WHY: only audit explicitly-labelled shell fences (```bash / ```sh).
    # Bare ``` fences in this README hold slash-command demos and JSON
    # config samples — auditing them as shell would misfire on every
    # `Then ...` prose line and every `{...}` JSON brace.
    pattern = re.compile(
        r"```(?:bash|sh)\n(?P<body>.*?)```",
        re.DOTALL,
    )
    return [m.group("body") for m in pattern.finditer(text)]


def _first_command_token(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    # Slash-command lines like "/issue-agent start" aren't shell — skip.
    if stripped.startswith("/"):
        return None
    # Strip leading shell prompts if any ("$ git status").
    if stripped.startswith("$ "):
        stripped = stripped[2:].lstrip()
    # First whitespace-separated token.
    return stripped.split(None, 1)[0]


def test_readme_code_blocks_reference_real_commands() -> None:
    text = _read_readme()
    blocks = _extract_shell_blocks(text)
    assert blocks, "README contains no shell code blocks — that's suspicious"

    offenders: list[tuple[str, str]] = []
    for block in blocks:
        for line in block.splitlines():
            token = _first_command_token(line)
            if token is None:
                continue
            # WHY: JSON / YAML inside a fenced block start with `{` or `-`.
            # We're only auditing shell-style first words here.
            if not re.match(r"^[A-Za-z0-9_./-]+$", token):
                continue
            if token not in ALLOWED_COMMANDS:
                offenders.append((token, line.strip()))

    assert not offenders, (
        "README references commands not used by this project "
        "(likely placeholders): "
        + ", ".join(f"{tok!r} in {line!r}" for tok, line in offenders)
    )
