"""TRD-015-TEST — full GitHub client wrapper.

PyGithub is a synchronous ``requests``-based library, so respx (which
mocks ``httpx``) cannot intercept its calls.  The cleanest equivalent
to "mock at the HTTP layer" is to monkeypatch
``Requester.requestJsonAndCheck`` — every PyGithub API call funnels
through that single method, so a single patch covers list / get / post
/ pulls uniformly.

Each test simulates exactly the response branch it cares about:

* Rate-limited 403 → :class:`GithubException` with ``X-RateLimit-Remaining: 0``
* 401 → ``GithubException(401, ...)``
* Happy-path → return ``(headers, body)`` and let PyGithub build real
  ``Issue`` objects on top of it.

These tests exist to prove three load-bearing properties:

1. The token never appears in raised error messages (REQ-023).
2. The token never appears in any captured log record during a failing
   call (REQ-023).
3. Rate-limit responses surface a parsed ``reset_at`` and the right
   ``ErrorCode`` (REQ-024 + REQ-025).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import pytest
from github import GithubException

from ghia import redaction
from ghia.errors import ErrorCode
from ghia.integrations.github import GitHubClient, GitHubClientError


# Obvious fake — kept clearly-not-real so any leak shows up trivially
# in a grep, and so this file isn't flagged by secret scanners.
_FAKE_TOKEN = "ghp_" + "z" * 36
_REPO = "octo/hello"


@pytest.fixture(autouse=True)
def _reset_logging_filters() -> None:
    """Strip filters left by the client so tests don't cross-pollute."""

    targets = [logging.getLogger(), logging.getLogger("github"),
               logging.getLogger("ghia.integrations.github")]
    before = {id(t): list(t.filters) for t in targets}
    redaction.set_token(None)
    yield
    for t in targets:
        for f in list(t.filters):
            if f not in before[id(t)]:
                t.removeFilter(f)
    redaction.set_token(None)


def _make_github_exception(
    status: int,
    *,
    message: str = "boom",
    headers: dict[str, str] | None = None,
) -> GithubException:
    """Build a :class:`GithubException` shaped like what PyGithub emits."""

    return GithubException(
        status=status,
        data={"message": message},
        headers=headers or {},
    )


def _patch_requester_to_raise(
    monkeypatch: pytest.MonkeyPatch, exc: GithubException
) -> None:
    """Monkeypatch every PyGithub HTTP call to raise ``exc``."""

    from github import Requester

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise exc

    monkeypatch.setattr(
        Requester.Requester, "requestJsonAndCheck", _raise, raising=True
    )


def _patch_requester_to_return(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    body: Any,
) -> None:
    """Make every PyGithub call return ``(headers, body)``."""

    from github import Requester

    def _ret(*args: Any, **kwargs: Any) -> Any:
        return (headers, body)

    monkeypatch.setattr(
        Requester.Requester, "requestJsonAndCheck", _ret, raising=True
    )


# ----------------------------------------------------------------------
# Construction + redaction wiring
# ----------------------------------------------------------------------


def test_construction_registers_token_for_redaction() -> None:
    GitHubClient(token=_FAKE_TOKEN, repo_full_name=_REPO)
    assert redaction.get_token() == _FAKE_TOKEN


def test_construction_rejects_empty_token() -> None:
    with pytest.raises(ValueError):
        GitHubClient(token="", repo_full_name=_REPO)


def test_construction_rejects_malformed_repo() -> None:
    with pytest.raises(ValueError):
        GitHubClient(token=_FAKE_TOKEN, repo_full_name="not-a-slug")


# ----------------------------------------------------------------------
# Rate-limit + token-invalid mappings
# ----------------------------------------------------------------------


async def test_token_never_appears_in_rate_limit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    future_epoch = int(time.time()) + 3600
    exc = _make_github_exception(
        403,
        message="API rate limit exceeded for token",
        headers={
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(future_epoch),
        },
    )
    _patch_requester_to_raise(monkeypatch, exc)

    client = GitHubClient(token=_FAKE_TOKEN, repo_full_name=_REPO)
    with pytest.raises(GitHubClientError) as ei:
        await client.list_issues()

    assert ei.value.code == ErrorCode.RATE_LIMITED
    assert isinstance(ei.value.reset_at, datetime)
    assert ei.value.reset_at.tzinfo == timezone.utc
    # Sanity check: the parsed time matches the epoch we sent.
    assert int(ei.value.reset_at.timestamp()) == future_epoch
    # Token must never escape via the exception text.
    assert _FAKE_TOKEN not in str(ei.value)
    assert _FAKE_TOKEN not in ei.value.message


async def test_rate_limit_without_reset_header_still_returns_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing reset header is informative-only — must not break the path."""

    exc = _make_github_exception(
        403,
        headers={"X-RateLimit-Remaining": "0"},
    )
    _patch_requester_to_raise(monkeypatch, exc)

    client = GitHubClient(token=_FAKE_TOKEN, repo_full_name=_REPO)
    with pytest.raises(GitHubClientError) as ei:
        await client.list_issues()

    assert ei.value.code == ErrorCode.RATE_LIMITED
    assert ei.value.reset_at is None
    assert "reset time unavailable" in ei.value.message.lower()


async def test_token_invalid_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    exc = _make_github_exception(401, message="Bad credentials")
    _patch_requester_to_raise(monkeypatch, exc)

    client = GitHubClient(token=_FAKE_TOKEN, repo_full_name=_REPO)
    with pytest.raises(GitHubClientError) as ei:
        await client.get_issue(1)

    assert ei.value.code == ErrorCode.TOKEN_INVALID
    assert _FAKE_TOKEN not in str(ei.value)


async def test_403_without_rate_limit_maps_to_token_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 with quota remaining is a permissions / SSO issue, not rate limit."""

    exc = _make_github_exception(
        403,
        message="Resource not accessible by integration",
        headers={"X-RateLimit-Remaining": "4999"},
    )
    _patch_requester_to_raise(monkeypatch, exc)

    client = GitHubClient(token=_FAKE_TOKEN, repo_full_name=_REPO)
    with pytest.raises(GitHubClientError) as ei:
        await client.list_issues()

    assert ei.value.code == ErrorCode.TOKEN_INVALID


async def test_404_maps_to_repo_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exc = _make_github_exception(404, message="Not Found")
    _patch_requester_to_raise(monkeypatch, exc)

    client = GitHubClient(token=_FAKE_TOKEN, repo_full_name=_REPO)
    with pytest.raises(GitHubClientError) as ei:
        await client.get_issue(99999)
    assert ei.value.code == ErrorCode.REPO_NOT_FOUND


async def test_unexpected_status_maps_to_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exc = _make_github_exception(503, message="Service Unavailable")
    _patch_requester_to_raise(monkeypatch, exc)

    client = GitHubClient(token=_FAKE_TOKEN, repo_full_name=_REPO)
    with pytest.raises(GitHubClientError) as ei:
        await client.list_issues()
    assert ei.value.code == ErrorCode.NETWORK_ERROR


async def test_connection_error_maps_to_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OS-level connect failures must surface as NETWORK_ERROR."""

    from github import Requester

    def _raise(*a: Any, **kw: Any) -> Any:
        raise ConnectionError("dns lookup failed")

    monkeypatch.setattr(
        Requester.Requester, "requestJsonAndCheck", _raise, raising=True
    )

    client = GitHubClient(token=_FAKE_TOKEN, repo_full_name=_REPO)
    with pytest.raises(GitHubClientError) as ei:
        await client.get_issue(1)
    assert ei.value.code == ErrorCode.NETWORK_ERROR


# ----------------------------------------------------------------------
# Logging redaction
# ----------------------------------------------------------------------


async def test_token_redacted_in_logs_during_call(
    monkeypatch: pytest.MonkeyPatch, captured_logger: Any
) -> None:
    """A failing API call must never leak the token through any log record.

    The client's ``_call`` wrapper logs at WARNING when it maps a
    ``GithubException``.  We trigger that path and assert the captured
    output contains zero verbatim copies of the token.  As a stronger
    check, we also force-log the token through this module's logger to
    prove the redaction filter is wired up — without it, the literal
    would survive into the captured messages.
    """

    exc = _make_github_exception(
        403,
        message=f"token {_FAKE_TOKEN} exceeded quota",
        headers={
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(int(time.time()) + 60),
        },
    )
    _patch_requester_to_raise(monkeypatch, exc)

    client = GitHubClient(token=_FAKE_TOKEN, repo_full_name=_REPO)

    # Sanity: the redaction filter sees the token now.
    assert redaction.get_token() == _FAKE_TOKEN

    with pytest.raises(GitHubClientError):
        await client.list_issues()

    # Force a record on this module's logger that contains the token.
    # If RedactionFilter isn't attached, this would survive verbatim.
    logging.getLogger("ghia.integrations.github").warning(
        "deliberate probe %s", _FAKE_TOKEN
    )

    for message in captured_logger.messages:
        assert _FAKE_TOKEN not in message, (
            f"token leaked in log message: {message!r}"
        )


# ----------------------------------------------------------------------
# Happy-path: list_issues returns plain dicts
# ----------------------------------------------------------------------


def _seed_repo_payload() -> dict[str, Any]:
    """The minimum keys PyGithub needs to construct a Repository."""

    return {
        "id": 1,
        "name": "hello",
        "full_name": _REPO,
        "owner": {"login": "octo", "id": 100, "type": "User"},
        "url": "https://api.github.com/repos/octo/hello",
        "html_url": "https://github.com/octo/hello",
    }


def _seed_issue_payload(
    number: int,
    *,
    title: str = "Issue",
    body: str = "Body",
    labels: list[str] | None = None,
    is_pr: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": number * 1000,
        "number": number,
        "title": title,
        "body": body,
        "state": "open",
        "user": {"login": "alice", "id": 1, "type": "User"},
        "labels": [
            {"id": i, "name": name, "color": "ededed"}
            for i, name in enumerate(labels or [])
        ],
        "assignees": [],
        "comments": 0,
        "html_url": f"https://github.com/octo/hello/issues/{number}",
        "url": f"https://api.github.com/repos/octo/hello/issues/{number}",
        "created_at": "2026-04-01T00:00:00Z",
        "updated_at": "2026-04-01T00:00:00Z",
    }
    if is_pr:
        payload["pull_request"] = {
            "url": f"https://api.github.com/repos/octo/hello/pulls/{number}"
        }
    return payload


async def test_list_issues_returns_dicts_with_required_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy-path 200 — wrapper returns plain dicts, never PyGithub objects."""

    from github import Requester

    repo_payload = _seed_repo_payload()
    issues = [
        _seed_issue_payload(1, title="bug one", labels=["bug"]),
        _seed_issue_payload(2, title="docs", labels=["documentation"]),
    ]
    pr_disguised = _seed_issue_payload(
        3, title="ignore me", is_pr=True
    )

    # PyGithub calls /repos/{full_name} first, then /repos/{full_name}/issues.
    # We dispatch on the URL substring so the same patch covers both.
    def _route(self: Any, verb: str, url: str, *args: Any, **kwargs: Any):
        if url.endswith("/repos/octo/hello"):
            return ({}, repo_payload)
        if "/issues" in url:
            return ({}, [*issues, pr_disguised])
        raise AssertionError(f"unexpected URL in test: {url}")

    monkeypatch.setattr(
        Requester.Requester, "requestJsonAndCheck", _route, raising=True
    )

    client = GitHubClient(token=_FAKE_TOKEN, repo_full_name=_REPO)
    result = await client.list_issues()

    # The PR-disguised entry must be filtered out.
    assert len(result) == 2
    assert all(isinstance(x, dict) for x in result)

    required = {
        "number", "title", "body", "labels", "html_url",
        "created_at", "updated_at", "author", "assignees", "comments_count",
    }
    for issue in result:
        assert required.issubset(issue.keys())

    # Spot-check shape: labels are list[str], not Label objects.
    assert result[0]["labels"] == ["bug"]
    assert result[0]["author"] == "alice"


async def test_get_issue_returns_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from github import Requester

    repo_payload = _seed_repo_payload()
    issue_payload = _seed_issue_payload(42, title="answer", labels=["bug"])

    def _route(self: Any, verb: str, url: str, *args: Any, **kwargs: Any):
        if url.endswith("/repos/octo/hello"):
            return ({}, repo_payload)
        if url.endswith("/issues/42"):
            return ({}, issue_payload)
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(
        Requester.Requester, "requestJsonAndCheck", _route, raising=True
    )

    client = GitHubClient(token=_FAKE_TOKEN, repo_full_name=_REPO)
    result = await client.get_issue(42)
    assert isinstance(result, dict)
    assert result["number"] == 42
    assert result["title"] == "answer"
    assert result["labels"] == ["bug"]


async def test_post_issue_comment_returns_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from github import Requester

    repo_payload = _seed_repo_payload()
    issue_payload = _seed_issue_payload(7, title="needs comment")
    comment_payload = {
        "id": 555,
        "html_url": "https://github.com/octo/hello/issues/7#issuecomment-555",
        "created_at": "2026-04-02T12:00:00Z",
        "body": "thanks!",
        "user": {"login": "bot", "id": 2, "type": "User"},
        "url": "https://api.github.com/repos/octo/hello/issues/comments/555",
        "issue_url": "https://api.github.com/repos/octo/hello/issues/7",
    }

    def _route(self: Any, verb: str, url: str, *args: Any, **kwargs: Any):
        if verb == "GET" and url.endswith("/repos/octo/hello"):
            return ({}, repo_payload)
        if verb == "GET" and url.endswith("/issues/7"):
            return ({}, issue_payload)
        if verb == "POST" and url.endswith("/issues/7/comments"):
            return ({}, comment_payload)
        raise AssertionError(f"unexpected call: {verb} {url}")

    monkeypatch.setattr(
        Requester.Requester, "requestJsonAndCheck", _route, raising=True
    )

    client = GitHubClient(token=_FAKE_TOKEN, repo_full_name=_REPO)
    result = await client.post_issue_comment(7, "thanks!")
    assert result["id"] == 555
    assert result["html_url"].endswith("issuecomment-555")


async def test_add_label_invokes_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from github import Requester

    repo_payload = _seed_repo_payload()
    issue_payload = _seed_issue_payload(3, labels=[])

    seen_calls: list[tuple[str, str]] = []

    def _route(self: Any, verb: str, url: str, *args: Any, **kwargs: Any):
        seen_calls.append((verb, url))
        if url.endswith("/repos/octo/hello"):
            return ({}, repo_payload)
        if url.endswith("/issues/3") and verb == "GET":
            return ({}, issue_payload)
        if "/labels" in url:
            # The label-add response shape doesn't matter for our wrapper.
            return ({}, [{"id": 1, "name": "ai-fix", "color": "ededed"}])
        raise AssertionError(f"unexpected: {verb} {url}")

    monkeypatch.setattr(
        Requester.Requester, "requestJsonAndCheck", _route, raising=True
    )

    client = GitHubClient(token=_FAKE_TOKEN, repo_full_name=_REPO)
    await client.add_label(3, "ai-fix")

    # Must have hit a labels endpoint.
    assert any("/labels" in url for _, url in seen_calls)


async def test_list_open_prs_returns_dicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from github import Requester

    repo_payload = _seed_repo_payload()
    pr_payload = [
        {
            "id": 1,
            "number": 100,
            "title": "Fix bug #42",
            "body": "Closes #42",
            "html_url": "https://github.com/octo/hello/pull/100",
            "url": "https://api.github.com/repos/octo/hello/pulls/100",
            "state": "open",
            "user": {"login": "dev", "id": 5, "type": "User"},
            "head": {
                "ref": "fix-issue-42",
                "sha": "deadbeef",
                "label": "octo:fix-issue-42",
                "user": {"login": "dev", "id": 5, "type": "User"},
                "repo": _seed_repo_payload(),
            },
            "base": {
                "ref": "main",
                "sha": "feedface",
                "label": "octo:main",
                "user": {"login": "octo", "id": 100, "type": "User"},
                "repo": _seed_repo_payload(),
            },
            "created_at": "2026-04-01T00:00:00Z",
            "updated_at": "2026-04-01T00:00:00Z",
        }
    ]

    def _route(self: Any, verb: str, url: str, *args: Any, **kwargs: Any):
        if url.endswith("/repos/octo/hello"):
            return ({}, repo_payload)
        if "/pulls" in url:
            return ({}, pr_payload)
        raise AssertionError(f"unexpected: {verb} {url}")

    monkeypatch.setattr(
        Requester.Requester, "requestJsonAndCheck", _route, raising=True
    )

    client = GitHubClient(token=_FAKE_TOKEN, repo_full_name=_REPO)
    prs = await client.list_open_prs()
    assert len(prs) == 1
    assert prs[0]["number"] == 100
    assert prs[0]["head_ref"] == "fix-issue-42"
    assert "Closes #42" in prs[0]["body"]
