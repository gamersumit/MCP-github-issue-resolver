"""Repo auto-detection tests (v0.2 refactor).

Two layers:

* :func:`parse_remote_url` — pure string parser; covered exhaustively
  here because the URL space (SSH / HTTPS / SSH-config-alias) is
  small and the regex is the only source of truth.
* :func:`detect_repo` — wraps ``git`` subprocess calls; covered via
  monkeypatched :func:`subprocess.run` so the suite stays
  network-and-binary-free.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from ghia.repo_detect import (
    RepoDetectionError,
    config_filename_for,
    detect_repo,
    parse_remote_url,
)


# ----------------------------------------------------------------------
# parse_remote_url
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        # SSH plain.
        ("git@github.com:owner/name.git", ("owner", "name")),
        ("git@github.com:owner/name", ("owner", "name")),
        # SSH-config alias.  Users with multiple GitHub accounts often
        # configure ~/.ssh/config with Host aliases like
        # ``github.com-work``; the URL keeps the alias form and we
        # must still extract the right slug.
        ("git@github.com-work:owner/name.git", ("owner", "name")),
        ("git@github.com-personal-2:owner/name", ("owner", "name")),
        # HTTPS.
        ("https://github.com/owner/name.git", ("owner", "name")),
        ("https://github.com/owner/name", ("owner", "name")),
        ("https://github.com/owner/name/", ("owner", "name")),
        ("http://github.com/owner/name", ("owner", "name")),
        # Names with dots / underscores / dashes (real-world repos).
        ("git@github.com:rust-lang/rust-clippy.git", ("rust-lang", "rust-clippy")),
        ("https://github.com/python/cpython", ("python", "cpython")),
        ("git@github.com:user.name/repo_name.git", ("user.name", "repo_name")),
    ],
)
def test_parse_remote_url_accepts_supported_shapes(
    url: str, expected: tuple[str, str]
) -> None:
    assert parse_remote_url(url) == expected


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        "   ",
        "git@gitlab.com:owner/name.git",
        "https://gitlab.com/owner/name.git",
        "https://example.com/owner/name",
        "not a url at all",
        # Missing the slash between owner and name.
        "git@github.com:ownername.git",
    ],
)
def test_parse_remote_url_rejects_invalid(bad_url: str) -> None:
    with pytest.raises(RepoDetectionError):
        parse_remote_url(bad_url)


def test_parse_remote_url_strips_whitespace() -> None:
    """Real ``git remote get-url`` output has a trailing newline."""

    assert parse_remote_url("  https://github.com/owner/name\n  ") == (
        "owner",
        "name",
    )


# ----------------------------------------------------------------------
# detect_repo (subprocess-mocked)
# ----------------------------------------------------------------------


def _make_git_run(
    *,
    rev_parse_rc: int = 0,
    rev_parse_stderr: str = "",
    remote_rc: int = 0,
    remote_stdout: str = "git@github.com:octo/hello.git\n",
    remote_stderr: str = "",
):
    """Build a ``subprocess.run`` stand-in that scripts the two git calls."""

    call_log: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        call_log.append(argv)
        # The repo_detect module always passes ``-C <root>`` first.
        # We only key off the subcommand verb to stay test-friendly.
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(
                argv, rev_parse_rc, stdout="/some/root\n", stderr=rev_parse_stderr
            )
        if "remote" in argv:
            return subprocess.CompletedProcess(
                argv, remote_rc, stdout=remote_stdout, stderr=remote_stderr
            )
        raise AssertionError(f"unexpected git call: {argv}")

    return fake_run, call_log


def test_detect_repo_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_run, _ = _make_git_run()
    monkeypatch.setattr(subprocess, "run", fake_run)

    owner, name = detect_repo(tmp_path)
    assert owner == "octo"
    assert name == "hello"


def test_detect_repo_https_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_run, _ = _make_git_run(
        remote_stdout="https://github.com/python/cpython.git\n"
    )
    monkeypatch.setattr(subprocess, "run", fake_run)

    owner, name = detect_repo(tmp_path)
    assert (owner, name) == ("python", "cpython")


def test_detect_repo_not_a_git_repo_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_run, _ = _make_git_run(
        rev_parse_rc=128,
        rev_parse_stderr="fatal: not a git repository",
    )
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RepoDetectionError) as info:
        detect_repo(tmp_path)
    assert "not inside a git repository" in str(info.value)


def test_detect_repo_no_origin_remote_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_run, _ = _make_git_run(
        remote_rc=2,
        remote_stderr="error: No such remote 'origin'",
    )
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RepoDetectionError) as info:
        detect_repo(tmp_path)
    msg = str(info.value)
    # The error must mention the missing origin AND give a helpful fix.
    assert "origin" in msg
    assert "git remote add" in msg


def test_detect_repo_git_missing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("git not on PATH")

    monkeypatch.setattr(subprocess, "run", boom)

    with pytest.raises(RepoDetectionError) as info:
        detect_repo(tmp_path)
    assert "git" in str(info.value).lower()


def test_detect_repo_non_github_remote_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_run, _ = _make_git_run(
        remote_stdout="https://gitlab.com/owner/repo.git\n"
    )
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RepoDetectionError):
        detect_repo(tmp_path)


# ----------------------------------------------------------------------
# config_filename_for
# ----------------------------------------------------------------------


def test_config_filename_for_uses_double_underscore_separator() -> None:
    assert config_filename_for("octo", "hello") == "octo__hello.json"
    assert config_filename_for("rust-lang", "rust-clippy") == (
        "rust-lang__rust-clippy.json"
    )


def test_config_filename_for_rejects_slashes_in_components() -> None:
    """Defensive: slashes would break the flat repos/ layout."""

    with pytest.raises(ValueError):
        config_filename_for("a/b", "c")
    with pytest.raises(ValueError):
        config_filename_for("a", "c/d")
