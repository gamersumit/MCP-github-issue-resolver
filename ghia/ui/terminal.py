"""Terminal-fallback picker (TRD-020).

Used when :func:`ghia.ui.opener.is_headless` returns True (no display,
SSH session, ``GHIA_FORCE_TERMINAL=1``).  Renders a `rich` table of
open issues, prompts for a comma-separated list of issue numbers, and
returns a dict matching the browser UI's ``POST /api/confirm``
contract: ``{"queue": [int...], "mode": "semi"|"full"}``.

Robustness rules:

* **The function never raises.**  Empty input returns an empty queue
  and the configured default mode; garbage input (``"abc, ;;"``) is
  parsed best-effort, surfacing a `[red]` warning and falling back to
  an empty queue.  We refuse to crash here because the caller is
  almost always a long-lived MCP server that should keep running.
* **Numbers not in the issue list are filtered out** with a warning so
  the user notices the typo rather than silently queuing nothing.
* **Issue numbers, not row indices.**  The table prints issue numbers
  in column 1; the user types those exact values.  Row indices would
  shift every fetch and confuse downstream tools.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from ghia.app import GhiaApp
from ghia.tools.issues import list_issues

logger = logging.getLogger(__name__)

__all__ = ["pick_issues_terminal"]


# Module-level console so tests can monkey-patch it for output capture.
_console = Console()


def _truncate(text: str, n: int = 80) -> str:
    """Trim a title to ``n`` chars with an ellipsis, for table display."""

    if not text:
        return ""
    return text if len(text) <= n else text[: n - 1] + "…"


def _parse_selection(
    raw: str, valid_numbers: set[int]
) -> tuple[list[int], list[str]]:
    """Parse a user-typed comma list into a deduped issue-number list.

    Tolerant by design: splits on commas AND whitespace, ignores empty
    chunks, ignores non-digit fragments (returning them in the
    ``ignored`` list so the caller can warn).  Numbers that *are*
    digits but aren't in the open-issue set are also returned in
    ``ignored`` — the user typed something the agent can't act on.

    Returns:
        ``(queue, ignored)`` — ``queue`` is the deduped list of valid
        ints in user-entered order; ``ignored`` is a list of the
        original tokens we couldn't honour.
    """

    if not raw:
        return [], []

    queue: list[int] = []
    seen: set[int] = set()
    ignored: list[str] = []

    # Replace commas with spaces so a single split() handles both
    # ``"1,3,5"`` and ``"1 3 5"`` and ``"1, 3, 5"``.
    for token in raw.replace(",", " ").split():
        token = token.strip()
        if not token:
            continue
        if not token.lstrip("+").isdigit():
            ignored.append(token)
            continue
        try:
            n = int(token)
        except ValueError:
            ignored.append(token)
            continue
        if n <= 0:
            ignored.append(token)
            continue
        if n not in valid_numbers:
            ignored.append(token)
            continue
        if n not in seen:
            seen.add(n)
            queue.append(n)

    return queue, ignored


def _build_table(issues: Iterable[dict[str, Any]]) -> Table:
    """Format the issue list as a `rich` table.

    Columns: ``#`` (number), ``priority``, ``title``.  Title is
    truncated to keep narrow terminals readable; the agent operates
    on numbers anyway.
    """

    table = Table(title="Open issues", show_lines=False)
    table.add_column("#", justify="right", style="bold cyan", no_wrap=True)
    table.add_column("priority", style="magenta", no_wrap=True)
    table.add_column("title")
    for issue in issues:
        number = str(issue.get("number", "?"))
        priority = str(issue.get("priority", "normal"))
        title = _truncate(str(issue.get("title", "")))
        table.add_row(number, priority, title)
    return table


async def pick_issues_terminal(app: GhiaApp) -> dict[str, Any]:
    """Render an interactive terminal picker and return the user's choices.

    Contract (mirrors the HTTP ``POST /api/confirm`` payload):

    * ``{"queue": [int, ...], "mode": "semi" | "full"}``
    * ``queue`` is empty (``[]``) when the user enters nothing or when
      every typed value was rejected.
    * ``mode`` defaults to :attr:`app.config.mode` when the user just
      presses ENTER.

    The function only ever writes to the local Console and reads from
    stdin — it does NOT mutate the session.  The caller (typically
    :func:`ghia.ui.opener.open_picker`) is responsible for persisting
    the returned dict to :class:`SessionStore`.  This separation lets
    the same dict shape be unit-tested without setting up a session
    file, and keeps the terminal path symmetrical with the browser
    path (where the ``/api/confirm`` route does the persisting).

    Args:
        app: The :class:`GhiaApp` whose config supplies the default
            mode and whose tools we call to fetch issues.

    Returns:
        Dict with keys ``queue`` (list of ints) and ``mode``
        (``"semi"`` or ``"full"``).  Always returns; never raises.
    """

    default_mode: str = (
        app.config.mode if app.config.mode in ("semi", "full") else "semi"
    )

    # Issue fetch — failures become an empty queue + warning rather
    # than an exception.  The user can always re-run the picker.
    try:
        resp = await list_issues(app)
    except Exception as exc:  # noqa: BLE001 — picker must not crash the server
        _console.print(f"[red]Failed to load issues: {exc}[/red]")
        return {"queue": [], "mode": default_mode}

    if not resp.success:
        _console.print(
            f"[red]Failed to load issues: {resp.error or 'unknown error'}[/red]"
        )
        return {"queue": [], "mode": default_mode}

    data: Optional[dict[str, Any]] = resp.data if isinstance(resp.data, dict) else None
    issues: list[dict[str, Any]] = list((data or {}).get("issues", []))

    if not issues:
        _console.print("[yellow]No open issues to pick.[/yellow]")
        return {"queue": [], "mode": default_mode}

    table = _build_table(issues)
    _console.print(table)

    valid_numbers = {int(i["number"]) for i in issues if "number" in i}

    # Prompt.ask returns "" when the user just hits enter.  We don't
    # set a default so the empty-string case is unambiguous.
    try:
        raw = Prompt.ask(
            "Enter issue numbers to queue (comma-separated, blank for none)",
            default="",
            show_default=False,
        )
    except (EOFError, KeyboardInterrupt):
        # Treat cancel as "no selection" — same as cancelling the browser.
        _console.print("[yellow]Cancelled — no issues queued.[/yellow]")
        return {"queue": [], "mode": default_mode}

    try:
        mode = Prompt.ask(
            "Mode",
            choices=["semi", "full"],
            default=default_mode,
            show_default=True,
        )
    except (EOFError, KeyboardInterrupt):
        mode = default_mode

    queue, ignored = _parse_selection(raw or "", valid_numbers)

    if ignored:
        _console.print(
            f"[red]Ignored {len(ignored)} unrecognised entr{'y' if len(ignored)==1 else 'ies'}: "
            f"{', '.join(ignored)}[/red]"
        )

    if mode not in ("semi", "full"):
        # Defensive: Prompt.ask with choices should make this impossible,
        # but a custom Console replacement in tests could bypass it.
        mode = default_mode

    return {"queue": queue, "mode": mode}
