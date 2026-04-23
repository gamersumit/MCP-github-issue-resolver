"""Test-runner and linter auto-detection (TRD-009).

Pure file-existence (plus TOML parsing) based detection.  We never
execute a binary — the goal is to *suggest* a sensible default for the
wizard to pre-fill.  The user is always free to override.

Detection order is deterministic and documented per ecosystem below.
When multiple ecosystems coexist in one repo (e.g. a Python backend
with an npm frontend), the Python rules win — the wizard is used from
the repo the agent will operate on, which is typically the server/tool
side.  Having the first match win (rather than returning a list of
choices) keeps the wizard UX single-question-per-field.

Confidence levels:
* ``high``   — an explicit configuration file pointed at the tool
  (``pyproject.toml [tool.pytest]``, ``package.json scripts.test``, …)
* ``medium`` — directory conventions only (``tests/`` without a
  pyproject entry, ``Gemfile`` without a ``spec/`` dir, …)
* ``low``    — reserved; not emitted today but part of the protocol so
  future rules can slot in without a schema change.

Satisfies REQ-002.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

# ``tomllib`` is stdlib on 3.11+; fall back to the ``tomli`` backport on
# 3.10 (which is also our minimum-supported interpreter).  Both expose
# the same ``loads`` / ``load`` surface so call sites don't care which
# is in scope.
if sys.version_info >= (3, 11):
    import tomllib as _toml  # type: ignore[import-not-found]
else:  # pragma: no cover - exercised on 3.10 in CI
    import tomli as _toml  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

__all__ = [
    "DetectedCommand",
    "DetectionResult",
    "detect",
]


@dataclass
class DetectedCommand:
    """A single suggested command with provenance."""

    command: str
    source_file: str
    confidence: str  # "high" | "medium" | "low"


@dataclass
class DetectionResult:
    """Combined output of one detection pass over a repo root."""

    test: Optional[DetectedCommand] = None
    lint: Optional[DetectedCommand] = None


def _read_toml(path: Path) -> Optional[Dict[str, Any]]:
    """Read a TOML file and return its parsed dict, or None on any error.

    We swallow *any* read/parse error — a malformed ``pyproject.toml``
    is not the detector's problem; we simply don't detect from it and
    let the user type the command manually.  This is called on files
    we've already proven exist via ``.exists()``.
    """

    try:
        with path.open("rb") as fh:
            return _toml.load(fh)
    except (OSError, _toml.TOMLDecodeError, ValueError) as exc:
        logger.debug("could not parse %s: %s", path, exc)
        return None


def _read_text(path: Path) -> Optional[str]:
    """Read a text file, returning None on failure."""

    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("could not read %s: %s", path, exc)
        return None


# ----------------------------------------------------------------------
# Test detection
# ----------------------------------------------------------------------


def _detect_test(repo_root: Path) -> Optional[DetectedCommand]:
    """Walk the priority-ordered rule set and return the first hit.

    Order matches the project spec: Python (highest priority because we
    *are* Python), then JavaScript, then the compiled-language
    ecosystems we're most likely to encounter.
    """

    # --- Python: pyproject-declared pytest -----------------------------
    pyproject = repo_root / "pyproject.toml"
    if pyproject.is_file():
        data = _read_toml(pyproject)
        if data is not None:
            tool_section = data.get("tool", {}) if isinstance(data, dict) else {}
            if isinstance(tool_section, dict) and "pytest" in tool_section:
                return DetectedCommand(
                    command="pytest -q",
                    source_file="pyproject.toml",
                    confidence="high",
                )
            # [project.optional-dependencies].dev including pytest
            project = data.get("project", {}) if isinstance(data, dict) else {}
            if isinstance(project, dict):
                opt = project.get("optional-dependencies", {})
                if isinstance(opt, dict):
                    dev = opt.get("dev", [])
                    if isinstance(dev, list) and any(
                        isinstance(item, str) and item.lower().startswith("pytest")
                        for item in dev
                    ):
                        return DetectedCommand(
                            command="pytest -q",
                            source_file="pyproject.toml",
                            confidence="high",
                        )

    # --- Python: pytest.ini or tests/ dir (medium) ---------------------
    if (repo_root / "pytest.ini").is_file():
        return DetectedCommand(
            command="pytest -q",
            source_file="pytest.ini",
            confidence="medium",
        )
    if (repo_root / "tests").is_dir():
        return DetectedCommand(
            command="pytest -q",
            source_file="tests/",
            confidence="medium",
        )

    # --- JavaScript / Node ---------------------------------------------
    package_json = repo_root / "package.json"
    if package_json.is_file():
        text = _read_text(package_json)
        if text is not None:
            # Cheap substring check dodges dependency on ``json`` module
            # behavior around duplicate keys, but stays correct because
            # ``"scripts"`` with a ``"test"`` entry is the documented
            # way to declare the test runner.
            try:
                import json as _json
                parsed = _json.loads(text)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                scripts = parsed.get("scripts", {})
                if isinstance(scripts, dict) and "test" in scripts:
                    return DetectedCommand(
                        command="npm test",
                        source_file="package.json",
                        confidence="high",
                    )

    # --- Rust -----------------------------------------------------------
    if (repo_root / "Cargo.toml").is_file():
        return DetectedCommand(
            command="cargo test",
            source_file="Cargo.toml",
            confidence="high",
        )

    # --- Go -------------------------------------------------------------
    if (repo_root / "go.mod").is_file():
        return DetectedCommand(
            command="go test ./...",
            source_file="go.mod",
            confidence="high",
        )

    # --- Ruby -----------------------------------------------------------
    if (repo_root / "Gemfile").is_file():
        if (repo_root / "spec").is_dir():
            return DetectedCommand(
                command="bundle exec rspec",
                source_file="Gemfile",
                confidence="high",
            )
        return DetectedCommand(
            command="bundle exec rake test",
            source_file="Gemfile",
            confidence="medium",
        )

    # --- Java / Maven / Gradle -----------------------------------------
    if (repo_root / "pom.xml").is_file():
        return DetectedCommand(
            command="mvn test",
            source_file="pom.xml",
            confidence="high",
        )
    if (repo_root / "build.gradle").is_file():
        return DetectedCommand(
            command="gradle test",
            source_file="build.gradle",
            confidence="high",
        )
    if (repo_root / "build.gradle.kts").is_file():
        return DetectedCommand(
            command="gradle test",
            source_file="build.gradle.kts",
            confidence="high",
        )

    # --- Elixir ---------------------------------------------------------
    if (repo_root / "mix.exs").is_file():
        return DetectedCommand(
            command="mix test",
            source_file="mix.exs",
            confidence="high",
        )

    return None


# ----------------------------------------------------------------------
# Lint detection
# ----------------------------------------------------------------------


def _detect_lint(repo_root: Path) -> Optional[DetectedCommand]:
    """Walk the priority-ordered lint rules and return the first hit."""

    # --- Python: ruff ---------------------------------------------------
    if (repo_root / "ruff.toml").is_file():
        return DetectedCommand(
            command="ruff check .",
            source_file="ruff.toml",
            confidence="high",
        )
    pyproject = repo_root / "pyproject.toml"
    if pyproject.is_file():
        data = _read_toml(pyproject)
        if isinstance(data, dict):
            tool_section = data.get("tool", {})
            if isinstance(tool_section, dict) and "ruff" in tool_section:
                return DetectedCommand(
                    command="ruff check .",
                    source_file="pyproject.toml",
                    confidence="high",
                )

    # --- Python: flake8 -------------------------------------------------
    if (repo_root / ".flake8").is_file():
        return DetectedCommand(
            command="flake8",
            source_file=".flake8",
            confidence="high",
        )
    setup_cfg = repo_root / "setup.cfg"
    if setup_cfg.is_file():
        text = _read_text(setup_cfg)
        if text is not None and "[flake8]" in text:
            return DetectedCommand(
                command="flake8",
                source_file="setup.cfg",
                confidence="high",
            )

    # --- JavaScript / Node: eslint -------------------------------------
    for candidate in (
        ".eslintrc",
        ".eslintrc.js",
        ".eslintrc.cjs",
        ".eslintrc.json",
        ".eslintrc.yaml",
        ".eslintrc.yml",
        "eslint.config.js",
        "eslint.config.cjs",
        "eslint.config.mjs",
        "eslint.config.ts",
    ):
        if (repo_root / candidate).is_file():
            return DetectedCommand(
                command="npx eslint .",
                source_file=candidate,
                confidence="high",
            )

    # --- Ruby: rubocop --------------------------------------------------
    if (repo_root / ".rubocop.yml").is_file():
        return DetectedCommand(
            command="bundle exec rubocop",
            source_file=".rubocop.yml",
            confidence="high",
        )

    # --- Go: golangci-lint ---------------------------------------------
    for name in (".golangci.yml", ".golangci.yaml"):
        if (repo_root / name).is_file():
            return DetectedCommand(
                command="golangci-lint run",
                source_file=name,
                confidence="high",
            )

    # --- Elixir: credo --------------------------------------------------
    mix_exs = repo_root / "mix.exs"
    if mix_exs.is_file():
        text = _read_text(mix_exs)
        if text is not None and ":credo" in text:
            return DetectedCommand(
                command="mix credo",
                source_file="mix.exs",
                confidence="high",
            )

    return None


def detect(repo_root: Path) -> DetectionResult:
    """Scan ``repo_root`` for a test runner and linter.

    Args:
        repo_root: Directory to scan.  Must exist; a missing directory
            returns an empty :class:`DetectionResult` rather than raising.

    Returns:
        :class:`DetectionResult` with whichever of ``test`` / ``lint``
        could be identified.  Either (or both) may be ``None``.
    """

    if not repo_root.exists() or not repo_root.is_dir():
        logger.debug("detect() called on non-directory %s", repo_root)
        return DetectionResult()

    return DetectionResult(
        test=_detect_test(repo_root),
        lint=_detect_lint(repo_root),
    )
