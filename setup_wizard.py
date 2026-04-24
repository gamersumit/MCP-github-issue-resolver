"""Interactive setup wizard (TRD-010).

Runs once per machine to populate ``~/.config/github-issue-agent/config.json``.
The wizard is deliberately a *bulk* collection — every prompt has a
default and the user can hit ENTER through the whole flow if the
detected values look right.

Key UX rules (AC-010-* / REQ-002):

* The token prompt never echoes the typed value.
* The token is never printed back in a summary panel.
* Detection runs before the first prompt so the user immediately sees
  sensible suggestions.
* A pre-existing config file is loaded and its values pre-fill the
  defaults so re-running the wizard is a safe no-op.
* Token and repo are validated live against the GitHub API before
  anything hits disk — bad tokens loop the prompt, bad repos loop the
  repo prompt.

The module is named ``setup_wizard.py`` — NOT ``setup.py`` — to avoid
confusion with the legacy setuptools entry point.  Invoke via
``python -m setup_wizard``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.text import Text

from ghia.config import (
    Config,
    ConfigMissingError,
    default_config_path,
    load_config,
    save_config,
)
from ghia.detection import DetectedCommand, DetectionResult, detect
from ghia.github_client_light import (
    TokenValidation,
    check_repo_access,
    validate_token,
)
from ghia.redaction import set_token
from ghia.tools.validation import InvalidCommandError, validate_command

logger = logging.getLogger(__name__)

_FINE_GRAINED_PAT_URL = "https://github.com/settings/personal-access-tokens/new"
_CLASSIC_PAT_URL = "https://github.com/settings/tokens/new"

# Matches GitHub's documented ``owner/name`` form.  Kept in sync with
# ``ghia.config._REPO_RE`` so the wizard rejects the same strings the
# model would reject later — users only see one error, not two.
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$")

# How many times the wizard will re-prompt the token / repo before
# giving up.  Users hitting this limit are either on a broken network
# or doing something adversarial — either way, bail.
_MAX_PROMPT_ATTEMPTS = 5


def _banner(console: Console) -> None:
    """Print the one-shot welcome panel."""

    body = Text()
    body.append(
        "This wizard sets up the github-issue-agent MCP server.\n\n",
        style="bold",
    )
    body.append(
        "It will ask for a GitHub personal access token and a target "
        "repository, then probe the GitHub API to confirm both work "
        "before writing anything to disk.\n\n"
    )
    body.append(
        "Config is stored at ~/.config/github-issue-agent/config.json "
        "(chmod 600). The token is never logged, printed, or echoed "
        "back after you type it."
    )
    console.print(Panel(body, title="github-issue-agent setup", expand=False))


def _load_existing(path: Path, console: Console) -> Optional[Config]:
    """Best-effort load of an existing config; surfaces errors gently."""

    if not path.exists():
        return None
    try:
        cfg = load_config(path=path)
    except ConfigMissingError as exc:
        console.print(
            f"[yellow]Existing config at {path} could not be loaded: "
            f"{exc}[/yellow]\n[yellow]Starting from scratch.[/yellow]"
        )
        return None
    console.print(
        f"[green]Loaded existing config from {path}.[/green] "
        "ENTER accepts the current value; type a new one to change it."
    )
    return cfg


def _format_detection(label: str, detected: Optional[DetectedCommand]) -> str:
    """Build the `[Detected: ...]` suffix shown after the field label."""

    if detected is None:
        return f"{label} (no automatic detection)"
    return (
        f"{label} [detected: {detected.command} via "
        f"{detected.source_file}, {detected.confidence} confidence]"
    )


def _prompt_with_default(
    console: Console,
    label: str,
    default: Optional[str],
    *,
    password: bool = False,
) -> str:
    """Wrap ``Prompt.ask`` with our default-display convention.

    Args:
        console: Rich console for consistent styling.
        label: The user-facing question.
        default: The default value if the user hits ENTER.  Always
            shown explicitly as ``(ENTER = <default>)`` unless the
            prompt is password-type, where we never leak the default.
        password: Whether to mask the typed value.

    Returns:
        The user's response (or the default).
    """

    prompt_text = label
    if default is not None and not password:
        prompt_text = f"{label} (ENTER = {default})"

    value = Prompt.ask(
        prompt_text,
        default=default,
        password=password,
        console=console,
        show_default=False,  # we render our own default display above
    )
    # Prompt.ask may return None for password prompts with no default.
    return (value or "").strip()


async def _prompt_token(
    console: Console, current: Optional[str]
) -> tuple[str, TokenValidation]:
    """Loop until the user gives us a GitHub token that validates.

    Returns:
        A tuple of (token, validation-result).  The validation result
        is returned alongside so the caller can surface scope warnings
        without re-hitting the network.
    """

    console.print(
        "\n[bold]GitHub token[/bold] — why: the agent calls the GitHub "
        "API (issues, PRs, commits) on your behalf, and clones repos "
        "via HTTPS using this token."
    )
    console.print(
        f"Generate a fine-grained PAT: [link]{_FINE_GRAINED_PAT_URL}[/link]"
    )
    console.print(
        f"Or a classic PAT (scope: [bold]repo[/bold]): "
        f"[link]{_CLASSIC_PAT_URL}[/link]"
    )
    if current is not None:
        console.print(
            "[dim]A token is already configured; ENTER to keep it.[/dim]"
        )

    for attempt in range(_MAX_PROMPT_ATTEMPTS):
        token = _prompt_with_default(
            console,
            "Paste token",
            default=current,  # falls through to keep-existing on ENTER
            password=True,
        )
        if not token:
            console.print("[red]Token cannot be empty. Try again.[/red]")
            continue

        console.print("[dim]Contacting GitHub to validate…[/dim]")
        result = await validate_token(token)
        if result.valid:
            if result.is_fine_grained:
                console.print(
                    "[green]Token accepted[/green] "
                    f"(user: [bold]{result.user or 'unknown'}[/bold], "
                    "fine-grained PAT)."
                )
                console.print(
                    "[yellow]Note: fine-grained PATs don't expose their "
                    "scope list. Make sure this token is scoped to the "
                    "single repo you want to work on and includes "
                    "Issues: R/W, Pull requests: R/W, Contents: R/W.[/yellow]"
                )
            else:
                console.print(
                    "[green]Token accepted[/green] "
                    f"(user: [bold]{result.user or 'unknown'}[/bold], "
                    f"scopes: {', '.join(result.scopes) or 'none'})."
                )
                if result.missing_scopes:
                    console.print(
                        f"[yellow]Missing scopes: "
                        f"{', '.join(result.missing_scopes)}. "
                        "The agent will fail on write operations without these."
                        "[/yellow]"
                    )
                    if not Confirm.ask(
                        "Continue anyway?",
                        default=False,
                        console=console,
                    ):
                        continue
            return token, result

        console.print(f"[red]Token rejected:[/red] {result.error}")
        # fall through to retry

    raise SystemExit(
        "Too many failed token attempts. Aborting setup — no config written."
    )


async def _prompt_repo(
    console: Console, token: str, current: Optional[str]
) -> str:
    """Loop until the user gives us a repo we can actually see."""

    console.print(
        "\n[bold]Target repository[/bold] (format: owner/name, e.g. octocat/hello-world)"
    )
    for attempt in range(_MAX_PROMPT_ATTEMPTS):
        repo = _prompt_with_default(console, "Repo", default=current)
        if not _REPO_RE.match(repo):
            console.print(
                "[red]Format must be 'owner/name' (letters, digits, dots, "
                "underscores, dashes). Try again.[/red]"
            )
            continue

        console.print("[dim]Checking repo access…[/dim]")
        result = await check_repo_access(token, repo)
        if result.valid:
            console.print(
                f"[green]Repo accessible[/green] "
                f"(full name: [bold]{result.user or repo}[/bold])."
            )
            return result.user or repo

        console.print(f"[red]{result.error}[/red]")
        # fall through to retry

    raise SystemExit(
        "Too many failed repo attempts. Aborting setup — no config written."
    )


def _prompt_label(console: Console, current: Optional[str]) -> str:
    """Prompt for the issue label with a default."""

    default = current if current else "ai-fix"
    value = _prompt_with_default(
        console, "\n[bold]Issue label[/bold]", default=default
    )
    return value or default


def _prompt_mode(console: Console, current: Optional[str]) -> str:
    """Prompt for the operating mode (semi/full)."""

    default = current if current in ("semi", "full") else "semi"
    while True:
        value = Prompt.ask(
            "\n[bold]Mode[/bold] — 'semi' approves each step, 'full' "
            f"runs end-to-end (ENTER = {default})",
            default=default,
            choices=["semi", "full"],
            console=console,
            show_choices=True,
            show_default=False,
        )
        if value in ("semi", "full"):
            return value
        console.print("[red]Must be 'semi' or 'full'.[/red]")


def _prompt_poll_interval(console: Console, current: Optional[int]) -> int:
    """Prompt for poll_interval_min (≥5 minutes)."""

    default = current if current is not None else 30
    while True:
        value = IntPrompt.ask(
            f"\n[bold]Poll interval (minutes)[/bold] — minimum 5 "
            f"(ENTER = {default})",
            default=default,
            console=console,
            show_default=False,
        )
        if value >= 5:
            return value
        console.print("[red]Poll interval must be at least 5 minutes.[/red]")


def _prompt_command(
    console: Console,
    kind: str,
    detected: Optional[DetectedCommand],
    current: Optional[str],
) -> Optional[str]:
    """Prompt for a test or lint command with detection + allow-list."""

    label = kind.capitalize() + " command"
    detection_line = _format_detection(label, detected)
    console.print(f"\n[bold]{detection_line}[/bold]")

    # Precedence for the default: the existing config value wins over
    # the detected value, because the user explicitly set it once.
    default = current if current else (detected.command if detected else "")
    prompt_text = (
        f"{kind.capitalize()} command"
        + (" (ENTER to skip)" if not default else f" (ENTER = {default})")
    )

    while True:
        raw = Prompt.ask(
            prompt_text,
            default=default,
            console=console,
            show_default=False,
        )
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            return validate_command(raw, kind=kind)
        except InvalidCommandError as exc:
            console.print(f"[red]{exc}[/red]")
            # loop — let the user fix it


def _success_panel(console: Console, path: Path, cfg: Config) -> None:
    """Print the final instructions panel.  NEVER prints the token."""

    body = Text()
    body.append(f"Config saved to {path}\n\n", style="bold green")
    body.append("Summary (token NOT shown):\n", style="bold")
    body.append(f"  repo:              {cfg.repo}\n")
    body.append(f"  label:             {cfg.label}\n")
    body.append(f"  mode:              {cfg.mode}\n")
    body.append(f"  poll_interval:     {cfg.poll_interval_min} min\n")
    body.append(f"  test_command:      {cfg.test_command or '(none)'}\n")
    body.append(f"  lint_command:      {cfg.lint_command or '(none)'}\n\n")
    body.append("Next steps:\n", style="bold")
    body.append("  1. Register the MCP server with Claude Code:\n")
    body.append(
        "     claude mcp add github-issue-agent -- python -m server\n",
        style="cyan",
    )
    body.append("  2. In Claude Code, run:\n")
    body.append("     /issue-agent start\n", style="cyan")
    console.print(Panel(body, title="Setup complete", expand=False))


async def main() -> int:
    """Entry point — return an int exit code."""

    console = Console()
    _banner(console)

    config_path = default_config_path()
    existing = _load_existing(config_path, console)

    # Run detection early so the test/lint prompts can use it as a
    # default.  repo_root is cwd — this is a developer running the
    # wizard from the project they want the agent to operate on.
    detection: DetectionResult = detect(Path.cwd())
    if detection.test or detection.lint:
        console.print(
            f"\n[dim]Detection against {Path.cwd()}:[/dim] "
            f"test={detection.test.command if detection.test else 'none'}; "
            f"lint={detection.lint.command if detection.lint else 'none'}"
        )

    try:
        token, _token_validation = await _prompt_token(
            console, existing.token if existing else None
        )
        # Register the token for redaction so any subsequent logs
        # (e.g. the repo-access probe's network errors) get scrubbed.
        set_token(token)

        repo = await _prompt_repo(
            console, token, existing.repo if existing else None
        )
        label = _prompt_label(console, existing.label if existing else None)
        mode = _prompt_mode(console, existing.mode if existing else None)
        poll = _prompt_poll_interval(
            console, existing.poll_interval_min if existing else None
        )
        test_cmd = _prompt_command(
            console,
            "test",
            detection.test,
            existing.test_command if existing else None,
        )
        lint_cmd = _prompt_command(
            console,
            "lint",
            detection.lint,
            existing.lint_command if existing else None,
        )
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Aborted by user. No config written.[/yellow]")
        return 130
    except SystemExit as exc:
        # SystemExit from the prompt loops — surface the message.
        console.print(f"[red]{exc}[/red]")
        return 1

    cfg = Config(
        token=token,
        repo=repo,
        label=label,
        mode=mode,  # type: ignore[arg-type]  # Literal narrowing
        poll_interval_min=poll,
        test_command=test_cmd,
        lint_command=lint_cmd,
    )
    save_config(cfg, path=config_path)
    _success_panel(console, config_path, cfg)
    return 0


def _run() -> int:
    """Sync wrapper for ``asyncio.run`` so setuptools entry points work."""

    return asyncio.run(main())


if __name__ == "__main__":
    sys.exit(_run())
