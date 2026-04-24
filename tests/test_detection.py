"""TRD-009-TEST — test runner / linter auto-detection.

Covers every ecosystem branch in :mod:`ghia.detection`, plus the
cross-ecosystem priority rule (Python wins over Node when both are
present) and the empty-tree fallback.

All fake repos are built inline under ``tmp_path`` with ``.write_text``
so there's no shared fixture state and each test is self-contained.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ghia.detection import DetectionResult, detect


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _touch(root: Path, relpath: str, content: str = "") -> Path:
    """Create ``root/relpath`` (including parents) and write ``content``."""

    target = root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


# ----------------------------------------------------------------------
# Test-runner detection
# ----------------------------------------------------------------------


def test_pyproject_with_tool_pytest_is_high_confidence(tmp_path: Path) -> None:
    _touch(
        tmp_path,
        "pyproject.toml",
        "[tool.pytest]\nminversion = '7.0'\n",
    )
    result = detect(tmp_path)
    assert result.test is not None
    assert result.test.command == "pytest -q"
    assert result.test.source_file == "pyproject.toml"
    assert result.test.confidence == "high"


def test_pyproject_dev_deps_pytest(tmp_path: Path) -> None:
    _touch(
        tmp_path,
        "pyproject.toml",
        (
            "[project]\nname='x'\nversion='0'\n"
            "[project.optional-dependencies]\n"
            "dev = ['pytest>=8', 'ruff']\n"
        ),
    )
    result = detect(tmp_path)
    assert result.test is not None
    assert result.test.command == "pytest -q"
    assert result.test.confidence == "high"


def test_pytest_ini_is_medium_confidence(tmp_path: Path) -> None:
    _touch(tmp_path, "pytest.ini", "[pytest]\n")
    result = detect(tmp_path)
    assert result.test is not None
    assert result.test.command == "pytest -q"
    assert result.test.source_file == "pytest.ini"
    assert result.test.confidence == "medium"


def test_tests_dir_only(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    result = detect(tmp_path)
    assert result.test is not None
    assert result.test.command == "pytest -q"
    assert result.test.confidence == "medium"


def test_package_json_scripts_test(tmp_path: Path) -> None:
    _touch(
        tmp_path,
        "package.json",
        '{"name":"x","scripts":{"test":"jest"}}',
    )
    result = detect(tmp_path)
    assert result.test is not None
    assert result.test.command == "npm test"
    assert result.test.source_file == "package.json"
    assert result.test.confidence == "high"


def test_package_json_without_scripts_test_is_skipped(tmp_path: Path) -> None:
    _touch(tmp_path, "package.json", '{"name":"x"}')
    result = detect(tmp_path)
    assert result.test is None


def test_cargo_toml_detects_cargo_test(tmp_path: Path) -> None:
    _touch(tmp_path, "Cargo.toml", "[package]\nname = 'x'\n")
    result = detect(tmp_path)
    assert result.test is not None
    assert result.test.command == "cargo test"
    assert result.test.source_file == "Cargo.toml"


def test_go_mod_detects_go_test(tmp_path: Path) -> None:
    _touch(tmp_path, "go.mod", "module example.com/x\n")
    result = detect(tmp_path)
    assert result.test is not None
    assert result.test.command == "go test ./..."


def test_gemfile_with_spec_dir_detects_rspec(tmp_path: Path) -> None:
    _touch(tmp_path, "Gemfile", "source 'https://rubygems.org'\n")
    (tmp_path / "spec").mkdir()
    result = detect(tmp_path)
    assert result.test is not None
    assert result.test.command == "bundle exec rspec"
    assert result.test.confidence == "high"


def test_gemfile_without_spec_dir_falls_back_to_rake(tmp_path: Path) -> None:
    _touch(tmp_path, "Gemfile", "source 'https://rubygems.org'\n")
    result = detect(tmp_path)
    assert result.test is not None
    assert result.test.command == "bundle exec rake test"
    assert result.test.confidence == "medium"


def test_pom_xml_detects_mvn_test(tmp_path: Path) -> None:
    _touch(tmp_path, "pom.xml", "<project></project>")
    result = detect(tmp_path)
    assert result.test is not None
    assert result.test.command == "mvn test"


def test_build_gradle_detects_gradle_test(tmp_path: Path) -> None:
    _touch(tmp_path, "build.gradle", "apply plugin: 'java'\n")
    result = detect(tmp_path)
    assert result.test is not None
    assert result.test.command == "gradle test"


def test_build_gradle_kts_detects_gradle_test(tmp_path: Path) -> None:
    _touch(tmp_path, "build.gradle.kts", "plugins { java }\n")
    result = detect(tmp_path)
    assert result.test is not None
    assert result.test.command == "gradle test"


def test_mix_exs_detects_mix_test(tmp_path: Path) -> None:
    _touch(tmp_path, "mix.exs", "defmodule X.MixProject do end\n")
    result = detect(tmp_path)
    assert result.test is not None
    assert result.test.command == "mix test"


def test_python_and_node_in_same_tree_python_wins(tmp_path: Path) -> None:
    """Priority rule: pytest beats npm test when both are present."""

    _touch(tmp_path, "pyproject.toml", "[tool.pytest]\n")
    _touch(
        tmp_path,
        "package.json",
        '{"scripts":{"test":"jest"}}',
    )
    result = detect(tmp_path)
    assert result.test is not None
    assert result.test.command == "pytest -q"
    # And specifically NOT npm test — don't silently pick the lower-priority one.
    assert result.test.command != "npm test"


def test_empty_tree_returns_no_detections(tmp_path: Path) -> None:
    result = detect(tmp_path)
    assert result.test is None
    assert result.lint is None


def test_nonexistent_root_returns_empty(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    result = detect(missing)
    assert isinstance(result, DetectionResult)
    assert result.test is None
    assert result.lint is None


def test_malformed_pyproject_is_ignored(tmp_path: Path) -> None:
    """A corrupted pyproject doesn't crash detection; falls through."""

    _touch(tmp_path, "pyproject.toml", "this is not valid toml {{{")
    # With no fallback signals → nothing detected
    result = detect(tmp_path)
    assert result.test is None


# ----------------------------------------------------------------------
# Linter detection
# ----------------------------------------------------------------------


def test_ruff_toml_detects_ruff(tmp_path: Path) -> None:
    _touch(tmp_path, "ruff.toml", "line-length = 100\n")
    result = detect(tmp_path)
    assert result.lint is not None
    assert result.lint.command == "ruff check ."
    assert result.lint.source_file == "ruff.toml"


def test_pyproject_tool_ruff_detects_ruff(tmp_path: Path) -> None:
    _touch(tmp_path, "pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    result = detect(tmp_path)
    assert result.lint is not None
    assert result.lint.command == "ruff check ."
    assert result.lint.source_file == "pyproject.toml"


def test_flake8_dotfile(tmp_path: Path) -> None:
    _touch(tmp_path, ".flake8", "[flake8]\nmax-line-length=100\n")
    result = detect(tmp_path)
    assert result.lint is not None
    assert result.lint.command == "flake8"


def test_setup_cfg_with_flake8_section(tmp_path: Path) -> None:
    _touch(tmp_path, "setup.cfg", "[flake8]\nmax-line-length=100\n")
    result = detect(tmp_path)
    assert result.lint is not None
    assert result.lint.command == "flake8"


def test_setup_cfg_without_flake8_section_is_skipped(tmp_path: Path) -> None:
    _touch(tmp_path, "setup.cfg", "[metadata]\nname=x\n")
    result = detect(tmp_path)
    assert result.lint is None


def test_eslintrc_detects_eslint(tmp_path: Path) -> None:
    _touch(tmp_path, ".eslintrc.json", "{}")
    result = detect(tmp_path)
    assert result.lint is not None
    assert result.lint.command == "npx eslint ."


def test_eslint_config_mjs_detects_eslint(tmp_path: Path) -> None:
    _touch(tmp_path, "eslint.config.mjs", "export default []")
    result = detect(tmp_path)
    assert result.lint is not None
    assert result.lint.command == "npx eslint ."


def test_rubocop_yml_detects_rubocop(tmp_path: Path) -> None:
    _touch(tmp_path, ".rubocop.yml", "AllCops:\n  NewCops: enable\n")
    result = detect(tmp_path)
    assert result.lint is not None
    assert result.lint.command == "bundle exec rubocop"


def test_golangci_yml_detects_golangci_lint(tmp_path: Path) -> None:
    _touch(tmp_path, ".golangci.yml", "linters:\n  enable:\n    - gofmt\n")
    result = detect(tmp_path)
    assert result.lint is not None
    assert result.lint.command == "golangci-lint run"


def test_mix_exs_with_credo_detects_credo(tmp_path: Path) -> None:
    _touch(
        tmp_path,
        "mix.exs",
        'defp deps do\n[{:credo, "~> 1.7", only: :dev}]\nend\n',
    )
    result = detect(tmp_path)
    assert result.lint is not None
    assert result.lint.command == "mix credo"


def test_mix_exs_without_credo_no_lint(tmp_path: Path) -> None:
    _touch(tmp_path, "mix.exs", "defmodule X.MixProject do end\n")
    result = detect(tmp_path)
    # Test still detected as mix test, but no lint
    assert result.test is not None
    assert result.lint is None
