"""TRD-008-TEST (part 2) — minimal GitHub client used at setup time.

Every case mocks the network via ``respx`` — no real HTTPS calls.  We
explicitly guard against a missing respx install by skipping with a
clear message instead of failing imports.
"""

from __future__ import annotations

import pytest

httpx = pytest.importorskip("httpx")
respx = pytest.importorskip("respx")

from ghia.github_client_light import (
    GITHUB_API_BASE,
    REQUIRED_CLASSIC_SCOPES,
    check_repo_access,
    validate_token,
)


# Obvious fakes — these never make it to the network; keeping them
# clearly-non-real so a grep for "ghp_" or "github_pat_" in source
# code doesn't flag this file as a leaked secret.
_CLASSIC_FAKE = "ghp_" + "x" * 36
_FINE_GRAINED_FAKE = "github_pat_" + "x" * 50


# ----------------------------------------------------------------------
# validate_token
# ----------------------------------------------------------------------


@respx.mock
async def test_validate_token_happy_path_classic_pat() -> None:
    route = respx.get(f"{GITHUB_API_BASE}/user").mock(
        return_value=httpx.Response(
            200,
            json={"login": "octocat", "id": 1},
            headers={"X-OAuth-Scopes": "repo, user:email"},
        )
    )

    result = await validate_token(_CLASSIC_FAKE)
    assert route.called
    assert result.valid is True
    assert result.user == "octocat"
    assert "repo" in result.scopes
    assert "user:email" in result.scopes
    assert result.is_fine_grained is False
    assert result.missing_scopes == []
    assert result.error is None


@respx.mock
async def test_validate_token_401_is_clear_error() -> None:
    respx.get(f"{GITHUB_API_BASE}/user").mock(
        return_value=httpx.Response(
            401, json={"message": "Bad credentials"}
        )
    )

    result = await validate_token(_CLASSIC_FAKE)
    assert result.valid is False
    assert result.error is not None
    assert "401" in result.error
    assert "revoked" in result.error.lower() or "correct" in result.error.lower()


@respx.mock
async def test_validate_token_fine_grained_flagged_even_on_200() -> None:
    respx.get(f"{GITHUB_API_BASE}/user").mock(
        return_value=httpx.Response(
            200,
            json={"login": "octocat"},
            # Fine-grained PATs don't set X-OAuth-Scopes
            headers={},
        )
    )

    result = await validate_token(_FINE_GRAINED_FAKE)
    assert result.valid is True
    assert result.is_fine_grained is True
    # Fine-grained: we don't compute missing_scopes
    assert result.missing_scopes == []
    assert result.scopes == []


@respx.mock
async def test_classic_token_missing_repo_scope() -> None:
    respx.get(f"{GITHUB_API_BASE}/user").mock(
        return_value=httpx.Response(
            200,
            json={"login": "octocat"},
            headers={"X-OAuth-Scopes": "user:email, read:org"},
        )
    )

    result = await validate_token(_CLASSIC_FAKE)
    assert result.valid is True
    assert result.missing_scopes == ["repo"]
    # Sanity: the required list hasn't been mutated
    assert REQUIRED_CLASSIC_SCOPES == ["repo"]


@respx.mock
async def test_validate_token_network_error_surfaces_clear_message() -> None:
    respx.get(f"{GITHUB_API_BASE}/user").mock(
        side_effect=httpx.ConnectError("DNS failure")
    )

    result = await validate_token(_CLASSIC_FAKE)
    assert result.valid is False
    assert result.error is not None
    assert "Network" in result.error or "network" in result.error


@respx.mock
async def test_validate_token_403_rate_limit_message() -> None:
    respx.get(f"{GITHUB_API_BASE}/user").mock(
        return_value=httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={"X-RateLimit-Remaining": "0"},
        )
    )

    result = await validate_token(_CLASSIC_FAKE)
    assert result.valid is False
    assert result.error is not None
    assert "rate limit" in result.error.lower()


async def test_validate_empty_token_fails_fast_without_network() -> None:
    # No respx mock — if the function hit the network, it'd fail.
    result = await validate_token("")
    assert result.valid is False
    assert result.error is not None
    assert "empty" in result.error.lower()


async def test_validate_whitespace_token_treated_as_empty() -> None:
    result = await validate_token("   ")
    assert result.valid is False


# ----------------------------------------------------------------------
# check_repo_access
# ----------------------------------------------------------------------


@respx.mock
async def test_check_repo_access_happy_path() -> None:
    respx.get(f"{GITHUB_API_BASE}/repos/octocat/hello-world").mock(
        return_value=httpx.Response(
            200,
            json={"full_name": "octocat/Hello-World", "private": False},
        )
    )

    result = await check_repo_access(_CLASSIC_FAKE, "octocat/hello-world")
    assert result.valid is True
    # The returned full_name preserves GitHub's canonical casing.
    assert result.user == "octocat/Hello-World"
    assert result.error is None


@respx.mock
async def test_check_repo_access_404_includes_repo_name() -> None:
    respx.get(f"{GITHUB_API_BASE}/repos/octocat/nope").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    result = await check_repo_access(_CLASSIC_FAKE, "octocat/nope")
    assert result.valid is False
    assert result.error is not None
    assert "octocat/nope" in result.error
    assert "not found or token lacks access" in result.error.lower()


@respx.mock
async def test_check_repo_access_401_is_auth_error() -> None:
    respx.get(f"{GITHUB_API_BASE}/repos/octocat/x").mock(
        return_value=httpx.Response(401)
    )

    result = await check_repo_access(_CLASSIC_FAKE, "octocat/x")
    assert result.valid is False
    assert result.error is not None
    assert "401" in result.error


@respx.mock
async def test_check_repo_access_403_fine_grained_hint() -> None:
    respx.get(f"{GITHUB_API_BASE}/repos/octocat/x").mock(
        return_value=httpx.Response(403)
    )

    result = await check_repo_access(_FINE_GRAINED_FAKE, "octocat/x")
    assert result.valid is False
    assert result.error is not None
    assert "403" in result.error
    # Fine-grained flag is surfaced even on failure so the wizard can
    # show the "enable this repo in your PAT's allow-list" hint.
    assert result.is_fine_grained is True


@respx.mock
async def test_check_repo_access_network_error() -> None:
    respx.get(f"{GITHUB_API_BASE}/repos/octocat/x").mock(
        side_effect=httpx.ConnectError("boom")
    )

    result = await check_repo_access(_CLASSIC_FAKE, "octocat/x")
    assert result.valid is False
    assert result.error is not None
    assert "Network" in result.error or "network" in result.error


async def test_check_repo_access_empty_repo_fails_fast() -> None:
    # No respx — must short-circuit before network
    result = await check_repo_access(_CLASSIC_FAKE, "")
    assert result.valid is False
    assert result.error is not None
    assert "empty" in result.error.lower()


# ----------------------------------------------------------------------
# Header correctness
# ----------------------------------------------------------------------


@respx.mock
async def test_validate_token_sends_expected_headers() -> None:
    route = respx.get(f"{GITHUB_API_BASE}/user").mock(
        return_value=httpx.Response(
            200, json={"login": "x"}, headers={"X-OAuth-Scopes": "repo"}
        )
    )

    await validate_token(_CLASSIC_FAKE)

    assert route.called
    request = route.calls.last.request
    assert request.headers.get("authorization") == f"token {_CLASSIC_FAKE}"
    assert "github" in request.headers.get("accept", "").lower()
    assert request.headers.get("user-agent", "").startswith(
        "github-issue-agent-setup"
    )
