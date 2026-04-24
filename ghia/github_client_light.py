"""Minimal GitHub HTTPS client used during setup (TRD-008).

A deliberately small wrapper around ``httpx`` that exposes just the two
endpoints the setup wizard needs:

* :func:`validate_token` — GET ``/user`` to prove the token works and
  surface the authenticated login + granted scopes (for classic PATs).
* :func:`check_repo_access` — GET ``/repos/{owner}/{name}`` to prove the
  token can actually see the repository the user typed in.

The full PyGithub-backed client lands in Cluster 4.  Until then we keep
the surface area minimal on purpose so that a bad token or a typo in
the repo slug can be caught before we persist anything to disk.

No token, repo slug, or other user-sensitive string is logged or echoed
back in an error message — the wizard reuses :func:`ghia.redaction.set_token`
once validation succeeds so subsequent logs are scrubbed.

Satisfies REQ-023 (token handling) and supports TRD-010 (wizard).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "TokenValidation",
    "validate_token",
    "check_repo_access",
    "REQUIRED_CLASSIC_SCOPES",
    "GITHUB_API_BASE",
    "FINE_GRAINED_PREFIX",
]


GITHUB_API_BASE: str = "https://api.github.com"
FINE_GRAINED_PREFIX: str = "github_pat_"

# Classic PATs expose their granted scopes via the X-OAuth-Scopes
# response header.  ``repo`` covers issues, pull_requests and contents
# (the three GitHub REST API resources we need write access to), so we
# only insist on that single scope even though ``repo`` is a superset.
REQUIRED_CLASSIC_SCOPES: List[str] = ["repo"]

# Short timeout — the wizard is interactive and the user should never
# feel like the process is hung waiting on the network.
_HTTP_TIMEOUT_SEC: float = 10.0


@dataclass
class TokenValidation:
    """Result of a single GitHub API probe.

    ``valid`` is the only field callers strictly need; the remaining
    fields are informative and populated on a best-effort basis.
    """

    valid: bool
    user: Optional[str] = None
    scopes: List[str] = field(default_factory=list)
    is_fine_grained: bool = False
    missing_scopes: List[str] = field(default_factory=list)
    error: Optional[str] = None


def _is_fine_grained(token: str) -> bool:
    """Return True iff ``token`` looks like a fine-grained PAT.

    Fine-grained PATs are prefixed with ``github_pat_``; classic tokens
    start with ``ghp_`` / ``gho_`` etc.  We branch on this because
    fine-grained PATs do NOT populate ``X-OAuth-Scopes`` — asking about
    scopes for them is meaningless.
    """

    return token.startswith(FINE_GRAINED_PREFIX)


def _parse_scopes(header_value: Optional[str]) -> List[str]:
    """Split a comma-separated ``X-OAuth-Scopes`` header into a list.

    GitHub returns values like ``"repo, user:email"`` — we strip each
    token and drop the empties so downstream consumers don't have to.
    Returns an empty list when the header is absent or blank.
    """

    if not header_value:
        return []
    return [s.strip() for s in header_value.split(",") if s.strip()]


def _missing_scopes(granted: List[str], required: List[str]) -> List[str]:
    """Return entries in ``required`` that aren't present in ``granted``.

    Preserves the order of ``required`` so error messages read
    consistently regardless of how GitHub chose to order its header.
    """

    granted_set = set(granted)
    return [s for s in required if s not in granted_set]


def _auth_headers(token: str) -> dict[str, str]:
    """Build the minimal headers for a GitHub API request."""

    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-issue-agent-setup/0.1",
    }


async def validate_token(token: str) -> TokenValidation:
    """Probe ``GET /user`` and interpret the response.

    Args:
        token: The PAT to test.  Stripped inside; callers may pass
            whitespace-padded copy-paste values.

    Returns:
        :class:`TokenValidation` — never raises on network failure;
        the failure is surfaced as ``valid=False`` with a human-readable
        ``error``.
    """

    token = (token or "").strip()
    is_fine_grained = _is_fine_grained(token)

    if not token:
        return TokenValidation(
            valid=False,
            is_fine_grained=is_fine_grained,
            error="Token is empty.",
        )

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SEC) as client:
            response = await client.get(
                f"{GITHUB_API_BASE}/user",
                headers=_auth_headers(token),
            )
    except httpx.TimeoutException as exc:
        return TokenValidation(
            valid=False,
            is_fine_grained=is_fine_grained,
            error=f"Network timeout contacting GitHub: {exc}",
        )
    except httpx.HTTPError as exc:
        return TokenValidation(
            valid=False,
            is_fine_grained=is_fine_grained,
            error=f"Network error: {exc}",
        )

    scopes = _parse_scopes(response.headers.get("X-OAuth-Scopes"))

    if response.status_code == 200:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        login = payload.get("login") if isinstance(payload, dict) else None
        missing: List[str] = []
        if not is_fine_grained:
            # Fine-grained PATs don't populate X-OAuth-Scopes; we can't
            # meaningfully check missing scopes for them.
            missing = _missing_scopes(scopes, REQUIRED_CLASSIC_SCOPES)
        return TokenValidation(
            valid=True,
            user=login,
            scopes=scopes,
            is_fine_grained=is_fine_grained,
            missing_scopes=missing,
            error=None,
        )

    if response.status_code == 401:
        return TokenValidation(
            valid=False,
            is_fine_grained=is_fine_grained,
            error=(
                "Token rejected by GitHub (401). "
                "Check it's correct and not revoked."
            ),
        )

    if response.status_code == 403:
        # 403 covers rate-limit, SSO-not-authorized, blocked org, etc.
        # The response body / headers tell us more; we surface that
        # rather than the raw status code alone.
        hint = response.headers.get("X-GitHub-SSO")
        if hint:
            msg = (
                "Token requires SSO authorization for this organization. "
                f"Authorize it: {hint}"
            )
        elif response.headers.get("X-RateLimit-Remaining") == "0":
            msg = "GitHub rate limit exceeded. Wait a few minutes and retry."
        else:
            msg = (
                "GitHub refused the request (403). "
                "The token may lack required scopes or be blocked by org policy."
            )
        return TokenValidation(
            valid=False,
            is_fine_grained=is_fine_grained,
            error=msg,
        )

    return TokenValidation(
        valid=False,
        is_fine_grained=is_fine_grained,
        error=(
            f"Unexpected GitHub response ({response.status_code}). "
            "Try again or check https://www.githubstatus.com/."
        ),
    )


async def check_repo_access(token: str, repo: str) -> TokenValidation:
    """Probe ``GET /repos/{repo}`` to confirm the token can see the repo.

    The returned :class:`TokenValidation` reuses the same shape as
    :func:`validate_token` — on success ``user`` is populated with the
    repo's full name (``owner/name``) so the caller can echo the
    authoritative casing back to the user.

    Args:
        token: The PAT to test.
        repo: The target repository in ``owner/name`` form.

    Returns:
        :class:`TokenValidation` — never raises.
    """

    token = (token or "").strip()
    is_fine_grained = _is_fine_grained(token)
    repo = (repo or "").strip()

    if not repo:
        return TokenValidation(
            valid=False,
            is_fine_grained=is_fine_grained,
            error="Repo is empty.",
        )

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SEC) as client:
            response = await client.get(
                f"{GITHUB_API_BASE}/repos/{repo}",
                headers=_auth_headers(token),
            )
    except httpx.TimeoutException as exc:
        return TokenValidation(
            valid=False,
            is_fine_grained=is_fine_grained,
            error=f"Network timeout contacting GitHub: {exc}",
        )
    except httpx.HTTPError as exc:
        return TokenValidation(
            valid=False,
            is_fine_grained=is_fine_grained,
            error=f"Network error: {exc}",
        )

    if response.status_code == 200:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        full_name = (
            payload.get("full_name") if isinstance(payload, dict) else None
        )
        return TokenValidation(
            valid=True,
            user=full_name,
            is_fine_grained=is_fine_grained,
            error=None,
        )

    if response.status_code == 404:
        return TokenValidation(
            valid=False,
            is_fine_grained=is_fine_grained,
            error=f"Repository {repo} not found or token lacks access.",
        )

    if response.status_code == 401:
        return TokenValidation(
            valid=False,
            is_fine_grained=is_fine_grained,
            error=(
                "Token rejected by GitHub (401). "
                "Check it's correct and not revoked."
            ),
        )

    if response.status_code == 403:
        return TokenValidation(
            valid=False,
            is_fine_grained=is_fine_grained,
            error=(
                f"Access to {repo} denied (403). "
                "Fine-grained PATs must include this repo in their allowed "
                "repositories list."
            ),
        )

    return TokenValidation(
        valid=False,
        is_fine_grained=is_fine_grained,
        error=(
            f"Unexpected GitHub response ({response.status_code}) "
            f"when accessing {repo}."
        ),
    )
