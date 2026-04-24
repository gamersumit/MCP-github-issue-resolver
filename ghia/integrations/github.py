"""Full GitHub API client wrapper (TRD-015).

A thin async facade over PyGithub.  Callers see only primitives and
plain ``dict`` results — no PyGithub types ever leak past this module's
boundary, which keeps the rest of the codebase free of upstream API
churn and lets tests mock the wrapper instead of the SDK.

Three concerns drive the design:

1. **Token safety (REQ-023)**.  Construction wires this module's logger
   through :class:`ghia.redaction.RedactionFilter` and re-registers the
   token via :func:`ghia.redaction.set_token` so any record emitted by
   PyGithub *or* this module is scrubbed.  Errors raised from this
   module build their messages from the API response only — never from
   request URLs or auth headers — so the token cannot appear in
   ``str(error)`` either.

2. **No blocking on the event loop**.  PyGithub is a synchronous
   library; every public method here off-loads its calls via
   :func:`asyncio.to_thread` so a slow API response never stalls the
   FastMCP event loop or other concurrent tools.

3. **Structured errors (REQ-025)**.  Every PyGithub exception is mapped
   to :class:`GitHubClientError` carrying one of the canonical
   :class:`ghia.errors.ErrorCode` values, so callers can ``return err(
   exc.code, exc.message)`` without further translation.  Rate-limit
   errors expose a parsed ``reset_at`` datetime so the queue processor
   can sleep precisely until quota resets.

Satisfies REQ-023 (token security), REQ-024 (network handling),
REQ-025 (structured errors).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from github import Auth, Github, GithubException

from ghia.errors import ErrorCode
from ghia.redaction import install_filter, set_token

logger = logging.getLogger(__name__)

# PyGithub emits its own log records via the ``github`` top-level
# logger.  We attach the redaction filter to that logger as well so a
# stray DEBUG line that accidentally echoes a header is still scrubbed
# before reaching any handler.
_PYGITHUB_LOGGER = logging.getLogger("github")

__all__ = [
    "GitHubClient",
    "GitHubClientError",
]


@dataclass
class GitHubClientError(Exception):
    """Structured error raised by every :class:`GitHubClient` method.

    Carries a canonical :class:`ErrorCode` so the calling tool can
    convert it directly to ``err(code, message)``.  ``reset_at`` is set
    only on rate-limit errors (otherwise ``None``) so callers can
    schedule a precise retry.

    Crucially, ``message`` is constructed from API-provided strings
    only — never from request URLs, header values, or anything else
    that could carry the token.  Combined with the redaction filter,
    that gives us defense-in-depth against token leakage through
    exception text.
    """

    code: ErrorCode
    message: str
    reset_at: Optional[datetime] = None

    def __str__(self) -> str:  # pragma: no cover — trivial
        return self.message


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _parse_reset_at(headers: Any) -> Optional[datetime]:
    """Convert the ``X-RateLimit-Reset`` header to a UTC datetime.

    GitHub returns the reset time as an integer epoch.  We accept any
    mapping-like ``headers`` and tolerate missing / malformed values by
    returning ``None`` — a missing reset time is informative-only and
    must not break the error path that's already firing.
    """

    if not headers:
        return None
    try:
        raw = headers.get("X-RateLimit-Reset") or headers.get(
            "x-ratelimit-reset"
        )
        if raw is None:
            return None
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _is_rate_limited(exc: GithubException) -> bool:
    """True iff the 403 actually represents rate-limit exhaustion.

    A 403 on the GitHub API can mean rate-limited, SSO-required, or
    insufficient permissions.  Only the ``X-RateLimit-Remaining: 0``
    branch maps to ``RATE_LIMITED``; everything else falls through to
    ``TOKEN_INVALID``.
    """

    headers = getattr(exc, "headers", None) or {}
    remaining = headers.get("X-RateLimit-Remaining") or headers.get(
        "x-ratelimit-remaining"
    )
    return str(remaining) == "0"


def _api_message(exc: GithubException) -> str:
    """Pull GitHub's own ``message`` field out of the response body.

    Falls back to a generic string when the body has no usable
    message.  We deliberately avoid ``str(exc)`` because PyGithub's
    default repr can include the full request URL — which contains the
    base URL but never the token.  Even so, building from ``data`` is
    the safer source.
    """

    data = getattr(exc, "data", None)
    if isinstance(data, dict):
        msg = data.get("message")
        if isinstance(msg, str) and msg:
            return msg
    return f"GitHub API returned status {exc.status}"


def _map_exception(exc: GithubException) -> GitHubClientError:
    """Translate a PyGithub exception to our structured error."""

    status = getattr(exc, "status", 0)
    headers = getattr(exc, "headers", None) or {}

    if status == 401:
        return GitHubClientError(
            ErrorCode.TOKEN_INVALID,
            f"GitHub rejected the token (401): {_api_message(exc)}",
        )

    if status == 403:
        if _is_rate_limited(exc):
            reset_at = _parse_reset_at(headers)
            if reset_at is not None:
                # Use a portable "YYYY-MM-DD HH:MM UTC" so logs and tool
                # responses read the same on every machine.
                when = reset_at.strftime("%Y-%m-%d %H:%M UTC")
                msg = (
                    f"GitHub rate limit exceeded; quota resets at {when}."
                )
            else:
                msg = "GitHub rate limit exceeded; reset time unavailable."
            return GitHubClientError(
                ErrorCode.RATE_LIMITED, msg, reset_at=reset_at
            )
        return GitHubClientError(
            ErrorCode.TOKEN_INVALID,
            f"GitHub refused the request (403): {_api_message(exc)}",
        )

    if status == 404:
        return GitHubClientError(
            ErrorCode.REPO_NOT_FOUND,
            f"GitHub returned 404: {_api_message(exc)}",
        )

    # Anything else (5xx, unexpected status) — treat as a transient
    # network-class failure so callers can retry / pause polling.
    return GitHubClientError(
        ErrorCode.NETWORK_ERROR,
        f"GitHub API error (status {status}): {_api_message(exc)}",
    )


def _label_names(labels: Any) -> list[str]:
    """Normalize PyGithub Label objects (or strings) to ``list[str]``.

    PyGithub's ``Issue.labels`` returns ``Label`` instances with a
    ``.name`` attribute; some API response paths hand back raw strings.
    We support both shapes so the wrapper stays robust to upstream
    changes.
    """

    out: list[str] = []
    if not labels:
        return out
    for item in labels:
        name = getattr(item, "name", None)
        if isinstance(name, str):
            out.append(name)
        elif isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            out.append(item["name"])
    return out


def _login_of(user: Any) -> Optional[str]:
    """Pull a ``login`` off a NamedUser-like object or return ``None``."""

    if user is None:
        return None
    return getattr(user, "login", None)


def _isoformat(value: Any) -> Optional[str]:
    """ISO-8601 string for a datetime; ``None`` passes through."""

    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _issue_to_dict(issue: Any) -> dict[str, Any]:
    """Render a PyGithub :class:`Issue` as a plain dict.

    Centralized so :meth:`GitHubClient.list_issues` and
    :meth:`GitHubClient.get_issue` always agree on the public shape.
    """

    return {
        "number": issue.number,
        "title": issue.title,
        "body": issue.body or "",
        "labels": _label_names(getattr(issue, "labels", None)),
        "html_url": issue.html_url,
        "created_at": _isoformat(getattr(issue, "created_at", None)),
        "updated_at": _isoformat(getattr(issue, "updated_at", None)),
        "author": _login_of(getattr(issue, "user", None)),
        "assignees": [
            _login_of(u)
            for u in (getattr(issue, "assignees", None) or [])
            if _login_of(u)
        ],
        "comments_count": getattr(issue, "comments", 0),
    }


def _comment_to_dict(comment: Any) -> dict[str, Any]:
    """Render a PyGithub :class:`IssueComment` as a plain dict."""

    return {
        "id": comment.id,
        "html_url": comment.html_url,
        "created_at": _isoformat(getattr(comment, "created_at", None)),
    }


def _pr_to_dict(pr: Any) -> dict[str, Any]:
    """Render a PyGithub :class:`PullRequest` as a plain dict."""

    head = getattr(pr, "head", None)
    head_ref = getattr(head, "ref", None) if head is not None else None
    return {
        "number": pr.number,
        "title": pr.title or "",
        "body": pr.body or "",
        "html_url": pr.html_url,
        "head_ref": head_ref,
    }


# ----------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------


class GitHubClient:
    """Async-friendly façade over PyGithub for one repository.

    One client wraps one ``owner/name`` — pass a different repo and
    build a different client.  PyGithub's ``Repository`` lookup is
    eager (one HTTP call) which is why we cache the resolved object
    after the first method call.
    """

    def __init__(self, token: str, repo_full_name: str) -> None:
        if not token:
            # Don't construct an unusable client — this is a programmer
            # error, not a runtime API failure.
            raise ValueError("GitHubClient requires a non-empty token")
        if not repo_full_name or "/" not in repo_full_name:
            raise ValueError(
                "GitHubClient requires a repo in 'owner/name' form"
            )

        # Wire redaction first so any subsequent log line — including
        # those PyGithub may emit during ``Github(...)`` construction —
        # is already scrubbed.
        set_token(token)
        install_filter(logger)
        install_filter(_PYGITHUB_LOGGER)

        self._token = token
        self._repo_full_name = repo_full_name
        # Construction itself does not hit the network; the repo lookup
        # is deferred to first use so a bad token surfaces inside an
        # actual API call (where we can map it to a structured error).
        self._gh = Github(auth=Auth.Token(token))
        self._repo: Any = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_repo(self) -> Any:
        """Resolve and cache the ``Repository`` handle.

        Called from inside ``asyncio.to_thread`` so the network round
        trip never blocks the event loop.  We deliberately do NOT
        catch exceptions here — the caller's ``_call`` wrapper
        translates them uniformly.
        """

        if self._repo is None:
            self._repo = self._gh.get_repo(self._repo_full_name)
        return self._repo

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run ``fn(*args, **kwargs)`` off-thread, mapping exceptions.

        Every public method funnels through here so error mapping and
        thread off-loading live in exactly one place.
        """

        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except GithubException as exc:
            mapped = _map_exception(exc)
            # Log at WARNING with the structured code so operators can
            # grep for failure modes without seeing the token.
            logger.warning(
                "GitHub API call failed: code=%s status=%s",
                mapped.code.value,
                getattr(exc, "status", "?"),
            )
            raise mapped from None
        except (TimeoutError, ConnectionError) as exc:
            logger.warning("GitHub API connection error: %s", type(exc).__name__)
            raise GitHubClientError(
                ErrorCode.NETWORK_ERROR,
                f"Could not reach GitHub: {type(exc).__name__}",
            ) from None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_issues(
        self,
        label: Optional[str] = None,
        state: str = "open",
    ) -> list[dict[str, Any]]:
        """Return issues for the repo, optionally filtered by label.

        PyGithub's ``Repository.get_issues`` returns *both* issues and
        pull requests because the GitHub API treats PRs as a special
        kind of issue.  We strip the PR results out so callers always
        see real issues only — checking for the ``pull_request``
        attribute (set on issue payloads that are actually PRs) is the
        canonical PyGithub idiom.
        """

        def _do() -> list[dict[str, Any]]:
            repo = self._get_repo()
            kwargs: dict[str, Any] = {"state": state}
            if label:
                kwargs["labels"] = [label]
            results: list[dict[str, Any]] = []
            for issue in repo.get_issues(**kwargs):
                # Skip pull-requests masquerading as issues.
                if getattr(issue, "pull_request", None) is not None:
                    continue
                results.append(_issue_to_dict(issue))
            return results

        return await self._call(_do)

    async def get_issue(self, number: int) -> dict[str, Any]:
        """Return one issue by number."""

        def _do() -> dict[str, Any]:
            repo = self._get_repo()
            return _issue_to_dict(repo.get_issue(number=number))

        return await self._call(_do)

    async def post_issue_comment(
        self, number: int, body: str
    ) -> dict[str, Any]:
        """Post a comment on an issue and return the created record."""

        def _do() -> dict[str, Any]:
            repo = self._get_repo()
            issue = repo.get_issue(number=number)
            comment = issue.create_comment(body)
            return _comment_to_dict(comment)

        return await self._call(_do)

    async def add_label(self, number: int, label: str) -> None:
        """Add a label to an issue.  No-op return — used by retry paths."""

        def _do() -> None:
            repo = self._get_repo()
            issue = repo.get_issue(number=number)
            issue.add_to_labels(label)

        await self._call(_do)

    async def list_open_prs(self) -> list[dict[str, Any]]:
        """Return all open pull requests on the repo as plain dicts."""

        def _do() -> list[dict[str, Any]]:
            repo = self._get_repo()
            return [_pr_to_dict(pr) for pr in repo.get_pulls(state="open")]

        return await self._call(_do)
