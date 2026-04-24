"""Interactive setup wizard (TRD-010, v0.2 refactor).

Runs once per repo to populate
``~/.config/github-issue-agent/repos/<owner>__<name>.json``.  The
wizard is still a bulk collection — every prompt has a default and
the user can hit ENTER through the whole flow if the detected values
look right.

v0.2 change: auth moves to the ``gh`` CLI.  The wizard now:

1. Verifies the cwd is inside a git repo.
2. Auto-detects ``owner/name`` from ``git remote get-url origin``.
3. Verifies ``gh`` is on PATH and authenticated.
4. Probes ``gh repo view`` to confirm the active gh account can see
   the repo.  On failure, prints the two most likely remedies:
   ``gh auth switch -u <other-account>`` and
   ``gh auth login --hostname github.com``.
5. Prompts for label / mode / poll_interval / test+lint commands.
6. Persists to the per-repo config path.

No token prompt anywhere — the PAT model is gone.  The wizard is
still password-safe by default (no secret-shaped values ever flow
through its input path), but the token-specific UX (never-echo,
scope-warnings) is moot because we don't handle tokens anymore.

The module is named ``setup_wizard.py`` — NOT ``setup.py`` — to avoid
confusion with the legacy setuptools entry point.  Invoke via
``python -m setup_wizard``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt
from rich.text import Text

from ghia.config import (
    Config,
    ConfigMissingError,
    config_path_for,
    load_config,
    save_config,
)
from ghia.detection import DetectedCommand, DetectionResult, detect
from ghia.integrations import gh_cli
from ghia.integrations.gh_cli import GhAuthError, GhUnavailable
from ghia.repo_detect import RepoDetectionError, detect_repo
from ghia.tools.validation import InvalidCommandError, validate_command

logger = logging.getLogger(__name__)


# How many times the wizard will re-prompt a command before giving up.
# Users hitting this limit are either confused or adversarial — either
# way, bail rather than looping forever.
_MAX_PROMPT_ATTEMPTS = 5


def _banner(console: Console) -> None:
    """Print the one-shot welcome panel."""

    body = Text()
    body.append(
        "This wizard sets up the github-issue-agent for the current repo.\n\n",
        style="bold",
    )
    body.append(
        "It auto-detects the repo from `git remote get-url origin` and "
        "uses your existing `gh` CLI authentication — no token prompts.\n\n"
    )
    body.append(
        "Config is stored at ~/.config/github-issue-agent/repos/"
        "<owner>__<name>.json (chmod 600), one file per repo so you can "
        "coexist multiple accounts cleanly.\n"
    )
    console.print(Panel(body, title="github-issue-agent setup", expand=False))


def _print_gh_install_help(console: Console) -> None:
    """Print install instructions for each common OS.

    Pulled into its own function so the error paths for "gh missing"
    and "gh misconfigured" can share the prose without duplicating it.
    """

    console.print(
        "[red]The `gh` CLI is not on PATH.[/red]\n"
        "Install it first, then re-run this wizard.\n\n"
        "  macOS:   [cyan]brew install gh[/cyan]\n"
        "  Debian:  [cyan]sudo apt install gh[/cyan] "
        "(or: see https://cli.github.com/)\n"
        "  Fedora:  [cyan]sudo dnf install gh[/cyan]\n"
        "  Windows: [cyan]winget install --id GitHub.cli[/cyan]\n"
        "  Other:   [link]https://cli.github.com/[/link]"
    )


def _load_existing(path: Path, console: Console) -> Optional[Config]:
    """Best-effort load of an existing per-repo config."""

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
) -> str:
    """Wrap ``Prompt.ask`` with our default-display convention."""

    prompt_text = label
    if default is not None:
        prompt_text = f"{label} (ENTER = {default})"

    value = Prompt.ask(
        prompt_text,
        default=default,
        console=console,
        show_default=False,  # we render our own default display above
    )
    return (value or "").strip()


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

    for _attempt in range(_MAX_PROMPT_ATTEMPTS):
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

    raise SystemExit(
        f"Too many invalid {kind}-command attempts. Aborting setup — "
        "no config written."
    )


def _success_panel(console: Console, path: Path, cfg: Config, repo: str, account: str) -> None:
    """Print the final instructions panel.

    The closing panel deliberately separates the two scopes the v0.1
    UX conflated: PER-REPO config (what we just wrote) vs the GLOBAL
    MCP server registration (done once per machine by ``install.sh``
    at user scope, NOT by this wizard). Spelling that out here avoids
    the v0.1 surprise of "I ran the wizard, why doesn't the slash
    command work in my OTHER repo".
    """

    body = Text()
    body.append(f"Setup complete for {repo}.\n\n", style="bold green")

    body.append("Per-repo config (THIS repo only):\n", style="bold")
    body.append(f"  {path}\n")
    body.append("  Stores: label, mode, poll_interval, test/lint commands.\n\n")

    body.append(
        "MCP server registration (GLOBAL — already done by install.sh):\n",
        style="bold",
    )
    body.append("  Scope: user\n")
    body.append("  No need to re-register per repo.\n\n")

    body.append("Active gh account in this shell: ", style="bold")
    body.append(f"{account}\n")
    body.append(
        "  (Switch with `gh auth switch -u <other>` before launching "
        "Claude Code if you need a different account.)\n\n"
    )

    body.append("Summary:\n", style="bold")
    body.append(f"  label:             {cfg.label}\n")
    body.append(f"  mode:              {cfg.mode}\n")
    body.append(f"  poll_interval:     {cfg.poll_interval_min} min\n")
    body.append(f"  test_command:      {cfg.test_command or '(none)'}\n")
    body.append(f"  lint_command:      {cfg.lint_command or '(none)'}\n\n")

    body.append("In Claude Code from this repo, you can:\n", style="bold")
    body.append("  - Type a slash command:\n")
    body.append("      /mcp__github-issue-agent__start\n", style="cyan")
    body.append("      /mcp__github-issue-agent__status\n", style="cyan")
    body.append(
        "      /mcp__github-issue-agent__set_mode <semi|full>\n", style="cyan"
    )
    body.append("      /mcp__github-issue-agent__stop\n", style="cyan")
    body.append("      /mcp__github-issue-agent__fetch_now\n", style="cyan")
    body.append("  - Or just ask Claude:\n")
    body.append('      "start the issue agent"\n', style="cyan")
    body.append('      "show issue-agent status"\n', style="cyan")
    body.append('      "switch to full mode"\n', style="cyan")
    body.append("\n")
    body.append(
        "If the MCP isn't registered yet, re-run `bash install.sh` from "
        "the agent's clone dir.\n",
        style="dim",
    )

    console.print(Panel(body, title="Setup complete", expand=False))


async def async_main() -> int:
    """Async wizard entry point — returns an int exit code.

    Kept async because every gh CLI call is awaited; the sync console-
    script entry (:func:`main`) wraps this with ``asyncio.run`` so the
    ``github-issue-agent-setup`` binary registered in pyproject.toml can
    invoke it without callers having to spin up an event loop themselves.
    """

    console = Console()
    _banner(console)

    # Step 1 + 2: auto-detect the repo.  ``detect_repo`` verifies the
    # cwd is a git repo AND pulls owner/name out of origin — one call
    # covers both failure modes with distinct error messages.
    try:
        owner, name = detect_repo(Path.cwd())
    except RepoDetectionError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2

    repo_full = f"{owner}/{name}"
    console.print(f"\n[bold]Repo:[/bold] {repo_full}")

    # Step 3: gh must be on PATH.
    if not gh_cli.gh_available():
        _print_gh_install_help(console)
        return 3

    # Step 4: gh must be authenticated on github.com.
    try:
        status = await gh_cli.auth_status()
    except GhUnavailable:
        # Race between gh_available and auth_status (someone removed
        # gh between the checks) — surface the same install help.
        _print_gh_install_help(console)
        return 3

    if not status["authenticated"]:
        console.print(
            "[red]`gh` is not authenticated.[/red]\n"
            "Run: [cyan]gh auth login --hostname github.com[/cyan]\n"
            "Then re-run this wizard."
        )
        return 4

    active_account = status["active_account"]
    # Edge case: gh is authenticated but the parser couldn't pick a login
    # out of the auth-status text (unfamiliar gh build, exotic locale,
    # color codes that defeated the regex).  Don't block the user — gh
    # itself confirmed they're logged in, so proceed with a warning and
    # let the subsequent ``repo_view`` call be the real authority on
    # whether the active account can do what we need.
    if active_account is None:
        console.print(
            "[yellow]`gh` is authenticated but the active account name "
            "could not be parsed from `gh auth status` output.[/yellow]\n"
            "[yellow]Continuing — repo access will be verified next.[/yellow]"
        )
    else:
        console.print(f"[bold]Active gh account:[/bold] {active_account}")

    # Step 5: verify the active account can actually see the repo.
    # The error path here is the most common "oh I'm logged in but
    # as the wrong account" situation, so the suggested-fixes message
    # matters more than the other branches.
    try:
        await gh_cli.repo_view(repo_full)
    except GhAuthError as exc:
        console.print(
            f"[red]Active gh account `{active_account}` cannot see "
            f"`{repo_full}`: {exc.message}[/red]\n"
            "Try one of:\n"
            f"  [cyan]gh auth switch -u <other-account>[/cyan]  "
            "(switch to an account with access)\n"
            "  [cyan]gh auth login --hostname github.com[/cyan]  "
            "(log in as a new account)"
        )
        return 5

    # Step 6: gather the 4 settings.  Detection runs on cwd so the
    # test/lint prompts have intelligent defaults.
    config_path = config_path_for(owner, name)
    existing = _load_existing(config_path, console)

    detection: DetectionResult = detect(Path.cwd())
    if detection.test or detection.lint:
        console.print(
            f"\n[dim]Detection against {Path.cwd()}:[/dim] "
            f"test={detection.test.command if detection.test else 'none'}; "
            f"lint={detection.lint.command if detection.lint else 'none'}"
        )

    try:
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
        label=label,
        mode=mode,  # type: ignore[arg-type]  # Literal narrowing
        poll_interval_min=poll,
        test_command=test_cmd,
        lint_command=lint_cmd,
    )
    save_config(cfg, path=config_path)
    _success_panel(console, config_path, cfg, repo_full, active_account or "unknown")
    return 0


def main() -> int:
    """Sync console-script entry — registered as ``github-issue-agent-setup``.

    Wraps :func:`async_main` with ``asyncio.run`` so the user can invoke
    ``github-issue-agent-setup`` from any repo dir after a one-time
    ``bash install.sh`` run. Also keeps ``python -m setup_wizard`` working
    via the ``__main__`` block below.
    """

    return asyncio.run(async_main())


# Back-compat alias so ``python -m setup_wizard`` (used by tests + by
# anyone scripting the wizard) keeps working without a Future warning.
_run = main


if __name__ == "__main__":
    sys.exit(main())
