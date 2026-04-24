"""TRD-INFRA-01-TEST — composition-root invariants for ``create_app``.

These tests sit alongside ``test_app.py`` (which already covers the
happy-path "returns a GhiaApp" + redaction-filter-attached cases) and
pin four invariants that are load-bearing for safe boot:

* The factory returns a fully-wired :class:`GhiaApp` whose ``session``
  field is a :class:`SessionStore` anchored at the right path — guards
  against accidental refactors that hand back a bare dataclass with
  ``session=None`` or a swapped path.
* The factory does **not** start the polling task implicitly. Polling
  is only allowed to start in response to an explicit
  ``issue_agent_start`` call; if ``create_app`` ever begins doing it
  itself, idle-by-default is silently broken.
* The redaction filter installed on the root logger actually scrubs
  token-shaped substrings via its regex safety net (v0.2: no token is
  registered, so the regex branch is the only line of defense).
* A relative ``repo_root`` path is normalized to absolute. Downstream
  path-guard logic relies on ``app.repo_root.is_absolute()`` so the
  factory must enforce it at the boundary.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Iterator

import pytest

from ghia import redaction
from ghia.app import create_app
from ghia.protocol import template_path
from ghia.session import SessionStore
from ghia.ui.server import picker_html_path


# ----------------------------------------------------------------------
# Local fixtures
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_redaction_state() -> Iterator[None]:
    """Detach any RedactionFilter this test attaches and clear the token.

    The composition root installs its filter on the root logger as a
    side effect; without this fixture, filters would accumulate across
    tests and leak token state between modules.
    """

    redaction.set_token(None)
    root = logging.getLogger()
    before = list(root.filters)
    yield
    for f in list(root.filters):
        if f not in before:
            root.removeFilter(f)
    redaction.set_token(None)


def _write_valid_config(path: Path, **overrides: object) -> dict:
    """Mirror the helper used by ``test_app.py`` (kept local — not exported).

    v0.2 schema: no token, no repo (auto-detected from git remote).
    Duplicating the literal here keeps these tests independent of any
    refactor of ``test_app.py``.
    """

    payload: dict = {
        "label": "ai-fix",
        "mode": "semi",
        "poll_interval_min": 30,
    }
    payload.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return payload


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


async def test_create_app_returns_wired_ghia_app(tmp_path: Path) -> None:
    """Every dataclass field must be populated AND session must be a real
    ``SessionStore`` pointing at ``<repo_root>/state/session.json``.

    Augments ``test_create_app_with_valid_config_returns_app`` which
    only checks ``config.repo`` / ``repo_root`` / ``logger``.
    """

    cfg_path = tmp_path / "cfg.json"
    _write_valid_config(cfg_path)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    app = await create_app(
        repo_root=repo_root, config_path=cfg_path, repo_full_name="octo/hello"
    )

    # All four "wired" fields populated — no None placeholders.
    assert app.config is not None
    assert app.session is not None
    assert app.repo_root is not None
    assert app.logger is not None

    # session is the right *type* (a refactor swapping in a stub would
    # otherwise pass earlier checks silently).
    assert isinstance(app.session, SessionStore)

    # session anchored at the repo-root-derived path. Re-resolve repo_root
    # because create_app stores the resolved form.
    expected = repo_root.resolve() / "state" / "session.json"
    assert app.session.path == expected


async def test_create_app_does_not_start_polling_implicitly(tmp_path: Path) -> None:
    """Idle-by-default invariant: ``_polling_task`` must be None.

    Polling is only allowed to start when ``issue_agent_start`` is
    invoked. If ``create_app`` ever began spawning the task itself,
    every ``claude mcp list`` would inadvertently start polling — and
    the user would have no way to keep the agent dormant for a config
    inspection.
    """

    cfg_path = tmp_path / "cfg.json"
    _write_valid_config(cfg_path)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    app = await create_app(
        repo_root=repo_root, config_path=cfg_path, repo_full_name="octo/hello"
    )

    # Direct attribute access (not a getattr default) so the test FAILS
    # loudly if the field is removed from the dataclass.
    assert app._polling_task is None


async def test_create_app_installed_filter_scrubs_token_shaped_strings(
    tmp_path: Path,
) -> None:
    """The installed filter must actually scrub token-shaped substrings.

    v0.2 dropped the PAT model — the agent no longer registers a
    specific token at create_app time.  But the redaction filter is
    still installed defensively so that if any subsystem (gh stderr,
    a misconfigured logger) accidentally echoes a token-shaped
    substring, the regex safety net catches it.

    We drive a synthetic token-shaped string through the root logger
    and assert that the regex branch of the filter scrubs it.
    """

    cfg_path = tmp_path / "cfg.json"
    _write_valid_config(cfg_path)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    await create_app(
        repo_root=repo_root, config_path=cfg_path, repo_full_name="octo/hello"
    )

    # Token-shaped string that matches the documented GitHub prefix
    # regex but was NEVER registered via ``set_token``.  The regex
    # safety net is the only thing that can scrub it — exactly the
    # property we want to verify.
    fake_token = "ghp_" + "z" * 40
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.DEBUG)

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    try:
        root.warning("leaking token=%s here", fake_token)
        handler.flush()
        output = buf.getvalue()
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    assert fake_token not in output, (
        f"raw token-shaped string leaked into log output: {output!r}"
    )
    assert redaction.REDACTED in output, (
        f"expected redaction marker in {output!r}"
    )


def test_shipped_assets_resolve_from_arbitrary_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: ``template_path`` and ``picker_html_path`` must locate
    their files regardless of the current working directory.

    The earlier packaging bug — assets living at the repo root rather
    than inside ``ghia/`` — produced wheels missing both files, and the
    failure only surfaced once the user ran the agent from a directory
    other than the repo. Pinning a CWD that is *not* the repo root
    catches that regression locally and post-install (the asserted
    invariant — "exists from anywhere" — is exactly what wheel users
    rely on).
    """

    # Anchor cwd somewhere that contains no ``prompts/`` or
    # ``ui_static/`` directory so a path-resolver that accidentally used
    # ``Path.cwd()`` (rather than ``__file__``) would silently fail this
    # test. ``tmp_path`` is guaranteed empty + ephemeral.
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "prompts").exists()
    assert not (tmp_path / "ui_static").exists()

    tpl = template_path()
    pkr = picker_html_path()

    # Both must point at real files. We use ``is_file`` (not just
    # ``exists``) so a stray directory at one of the paths would fail.
    assert tpl.is_file(), (
        f"agent protocol template not resolvable from {tmp_path}: {tpl}"
    )
    assert pkr.is_file(), (
        f"picker.html not resolvable from {tmp_path}: {pkr}"
    )

    # Defensive sanity: assets must be absolute so callers that pass
    # them across boundaries (e.g. into Starlette's FileResponse) get
    # deterministic behaviour.
    assert tpl.is_absolute()
    assert pkr.is_absolute()


async def test_create_app_resolves_repo_root_to_absolute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative ``repo_root`` must be normalized to an absolute path.

    Downstream path-guard checks (e.g. ``ghia.paths.ensure_within``)
    assume ``app.repo_root.is_absolute()`` so they can compute parent
    chains without ``ValueError``. ``create_app`` is the boundary
    where that contract is enforced.
    """

    cfg_path = tmp_path / "cfg.json"
    _write_valid_config(cfg_path)

    # Build a real directory at tmp_path/repo so the resolved path is
    # meaningful even though we PASS the relative form to create_app.
    (tmp_path / "repo").mkdir()

    # Chdir into tmp_path so "repo" resolves under it. monkeypatch.chdir
    # auto-restores cwd at teardown — important because pytest's other
    # tests assume the original working directory.
    monkeypatch.chdir(tmp_path)

    relative = Path("repo")
    assert not relative.is_absolute(), "precondition: input must be relative"

    app = await create_app(
        repo_root=relative, config_path=cfg_path, repo_full_name="octo/hello"
    )

    assert app.repo_root.is_absolute(), (
        f"create_app must resolve relative paths; got {app.repo_root!r}"
    )
    # Sanity: the resolved form points at the directory we created.
    assert app.repo_root == (tmp_path / "repo").resolve()
