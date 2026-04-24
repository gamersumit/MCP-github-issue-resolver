# github-issue-agent

**MCP server that lets Claude Code resolve GitHub issues end-to-end — from backlog to open PR.**

Drops into Claude Code as a local Model Context Protocol (MCP) server, stays idle until you ask it to start, opens a browser picker for selecting issues, and either walks you through each fix (semi-auto) or drives PRs to completion autonomously (full-auto).

---

## Status

| Sprint | Scope | Status |
|---|---|---|
| Sprint 1a — Foundation (TRD-001..006) | error types, token redaction, atomic writes, path guards, config, session state | Shipped |
| Sprint 1b — Core Control (TRD-011..014, TRD-033) | MCP bootstrap, control tools, protocol renderer, convention discovery | Shipped |
| Sprint 2 — Setup wizard + GitHub client (TRD-007..010) | install.sh, wizard, token validation, auto-detection | Shipped |
| Sprint 3 — Picker UI + duplicate detection (TRD-015..021) | Starlette picker on `127.0.0.1:4242`, `rich` terminal fallback, headless auto-detection | Shipped |
| Sprint 4 — Code & Git ops (TRD-022..030) | Shipped — fs/git/PR tools, native-CLI git wrapper with PyGithub fallback, Docker-sandboxed `run_tests`, allow-listed `check_linting`, `undo_last_change` with author-email guard. |
| Sprint 5 — Polish & Ship (TRD-031..035) | Shipped — serial queue processor, polling timer with jitter, retry policy, README + packaging polish. |

**479 tests** pass in ~19s. Coverage is enforced ≥ 80% at sprint exit. All MCP tools listed below are wired and reachable from Claude Code.

> **v0.2 breaking change.** PAT-based config is gone — auth now flows through your existing `gh` CLI session, repos are auto-detected from `git remote get-url origin`, and config is per-repo (`~/.config/github-issue-agent/repos/<owner>__<name>.json`). If you upgraded from v0.1, your old `~/.config/github-issue-agent/config.json` is ignored — run `github-issue-agent-setup` once per repo to migrate.

See `docs/PRD/PRD-2026-001-github-issue-agent.md` and `docs/TRD/TRD-2026-001-github-issue-agent.md` for the full specification and technical design.

---

## Where to run what

The setup splits into three ceremonies — each has a fixed place to run and a fixed frequency. Following the table avoids the v0.1 surprise of "I ran the wizard inside the AGENT'S clone and now it's mis-configured".

| Ceremony | Where to run | Frequency | Purpose |
|---|---|---|---|
| `bash install.sh` | Inside the agent clone dir | ONCE per machine | Install deps, register MCP at USER scope (works in every project) |
| `github-issue-agent-setup` | Inside ANY target repo | Once per target repo | Detect repo + active gh account, write per-repo config |
| `/mcp__github-issue-agent__start` (or natural language) | Inside target repo, in Claude Code | Each session | Run the agent |

You should NEVER need to remember the agent's venv path, run the wizard inside the agent's own clone, or re-register the MCP per project.

---

## Install

> **Once per machine.** Clones the agent, builds its venv, registers the MCP at user scope.

```bash
git clone https://github.com/gamersumit/MCP-github-issue-resolver.git ~/tools/github-issue-agent
cd ~/tools/github-issue-agent
bash install.sh
```

What this does:

- Creates a venv and installs deps
- Registers the MCP server with Claude Code at **user** scope (so it works in every project, not just the clone dir)
- Installs two console scripts: `github-issue-agent` (the MCP server) and `github-issue-agent-setup` (the per-repo wizard)

The installer is idempotent — re-running it on a machine that already has the MCP registered detects the existing registration and fixes the scope if needed.

### Prerequisites

- **Python 3.10+** (stdlib `tomllib` is auto-used on 3.11+, `tomli` backport pulled in for 3.10)
- **Git** on `$PATH` — default branch is auto-detected per repo (no `main` / `master` hardcoding); also used to detect `origin` so the agent knows what repo it's working on
- **`gh` CLI** on `$PATH` and authenticated — **required** in v0.2; this is where the agent gets its GitHub credentials. Run `gh auth login --hostname github.com` once and the agent inherits the active account on every call
- **Claude Code** CLI — [install guide](https://docs.claude.com/en/docs/claude-code) — for MCP registration
- **Docker** — optional but required for `run_tests` (sandboxed test execution); `run_tests` returns `DOCKER_UNAVAILABLE` with install hint when the daemon is unreachable

---

## Per-repo setup (once per repo you want the agent to handle)

```bash
cd ~/path/to/your/target-repo
github-issue-agent-setup
```

What this does:

- Auto-detects the repo from `git remote get-url origin`
- Verifies your `gh` CLI is authenticated and the active account can see the repo
- Saves config to `~/.config/github-issue-agent/repos/<owner>__<name>.json`
- **Never** prompts for a token — it inherits your `gh` CLI auth

To swap accounts before running the wizard (e.g. you're working on a repo owned by a different GitHub user), use `gh auth switch -u <other-account>` first.

---

## Use it (each time, from inside the target repo)

```bash
cd ~/path/to/your/target-repo
claude
```

Then in Claude Code, either:

```
/mcp__github-issue-agent__start
```

or just say:

```
start the issue agent
```

Happy path you'll see:

1. Claude reports the active mode (`semi` or `full`) and the rendered protocol header.
2. Claude reads any of `CLAUDE.md`, `CONTRIBUTING.md`, `AGENTS.md`, `.cursor/rules/*.md`, `.editorconfig`, top-level `README.md` that exist and prints a **conventions summary** (cached for the session).
3. The browser picker opens at `http://127.0.0.1:4242/` showing every issue tagged with your configured label (`ai-fix` by default), priority-sorted. Headless / SSH session? You'll automatically get a `rich` terminal table instead — pick by typing the issue numbers.
4. For each picked issue, Claude follows the workflow in `ghia/prompts/agent_protocol.md`:
   - `git create_branch` → `fix/issue-{n}-{slug}` (refuses if the slug ever resolves to the default branch)
   - `read_file` / `search_codebase` / `get_repo_structure` to investigate
   - `write_file` (atomic — temp + fsync + rename) to fix
   - `check_linting` then `run_tests` (Docker sandbox, repo mounted read-only, runs as `nobody`)
   - `commit_changes` → `push_branch` → `create_pr` (draft in full-auto, regular in semi)
   - `post_issue_comment` with PR link
5. In **semi**, Claude pauses for your approval before each step. In **full**, Claude runs end-to-end and stops at PR open (never auto-merges).

When you want to step away:

```
/mcp__github-issue-agent__stop
```

This pauses the agent — the queue, completed/skipped counters, and conventions summary all survive across `start` calls.

---

## Quick Start

```bash
# Once per machine
gh auth login --hostname github.com

# Once per target repo
cd /path/to/your/repo
github-issue-agent-setup
```

Then open Claude Code in the repo dir and either type the slash command:

```
/mcp__github-issue-agent__start
```

…or just say `start the issue agent` in natural language.

---

## Commands

### MCP tools (registered with FastMCP, callable from Claude Code)

Slash commands take the form `/mcp__github-issue-agent__<name>` — Claude Code generates this naming pattern automatically from each registered FastMCP prompt. You can also drive every tool below in natural language ("show issue-agent status", "switch to full mode", etc.).

| Slash command | Tool name | What it does |
|---|---|---|
| `/mcp__github-issue-agent__start` | `issue_agent_start` | Activates agent, runs Step 0 (convention discovery), injects workflow protocol, kicks off polling timer. |
| `/mcp__github-issue-agent__stop` | `issue_agent_stop` | Pauses agent, cancels polling task, preserves session state. |
| `/mcp__github-issue-agent__status` | `issue_agent_status` | Returns full `SessionState` + human-readable summary (mode, queue depth, last-fetch timestamp). |
| `/mcp__github-issue-agent__set_mode <semi\|full>` | `issue_agent_set_mode` | Switches between `"semi"` and `"full"`. Takes effect **immediately** at the next decision point — in-flight work is preserved. |
| `/mcp__github-issue-agent__fetch_now` | `issue_agent_fetch_now` | Force an immediate issue refresh, bypassing the poll interval. |

Internal tools the protocol drives (Claude calls these automatically — you don't type them):

`list_issues`, `get_issue`, `pick_issue`, `skip_issue`, `post_issue_comment`, `check_issue_has_open_pr`, `read_file`, `write_file`, `list_directory`, `search_codebase`, `get_repo_structure`, `read_multiple_files`, `create_branch`, `git_diff`, `commit_changes`, `push_branch`, `get_current_branch`, `get_default_branch`, `create_pr`, `run_tests`, `check_linting`, `undo_last_change`.

### Setup wizard

```bash
cd /path/to/your/repo
github-issue-agent-setup
```

Run from the repo directory any time you want to change the label, switch default mode, adjust the poll interval, or override the auto-detected test/lint commands. The wizard:

1. Reads `git remote get-url origin` to identify the repo
2. Confirms your active `gh` account can see it
3. Reads any existing per-repo config and pre-fills every prompt — ENTER accepts the current value

To swap accounts (e.g. you're working on a repo owned by a different GitHub user), use `gh auth switch -u <other-account>` first; the very next `github-issue-agent-setup` run (and every subsequent agent call) uses the new account automatically.

---

## Modes

You can be in exactly one mode at a time. Switch mid-session with `/mcp__github-issue-agent__set_mode semi` or `/mcp__github-issue-agent__set_mode full` (or just say "switch to full mode").

| Aspect | `semi` (default) | `full` |
|---|---|---|
| Approval before each step | Yes — Claude proposes, you confirm | No — runs end-to-end |
| PR draft state | Regular PR | **Draft PR** (REQ-018 — never auto-merges) |
| Retry budget on test/lint failure | Manual (you decide) | 3 attempts per issue, then `skip_issue` |
| In-flight work on switch | Preserved — switch applies at next decision point | Same — the current issue finishes its current step before mode flips |
| Best for | Sensitive repos, unfamiliar codebases | Routine `ai-fix`-labelled bugs you've already triaged |

The mode is read at every decision point in `ghia/prompts/agent_protocol.md` (rendered fresh per call), so flipping modes does not require restarting the agent.

---

## Token how-to

**v0.2 doesn't use a token directly.** Auth is delegated to the [**`gh` CLI**](https://cli.github.com/), which holds your GitHub credentials in the OS keychain (or its own config tree). The agent shells out to `gh` for every GitHub API call and inherits whichever account is currently active.

### Install gh

```bash
brew install gh                         # macOS
sudo apt install gh                     # Debian / Ubuntu
sudo dnf install gh                     # Fedora
```

Windows or other platforms: see [cli.github.com](https://cli.github.com/).

### Log in

```bash
gh auth login --hostname github.com
```

The interactive flow walks you through choosing HTTPS vs SSH, picking an authentication method (browser / token paste / key), and stashing the credential in your keychain. You only have to do this once per account per machine.

### Multi-account workflow

If you contribute to repos owned by different GitHub accounts (e.g. personal + work), log in to each account once:

```bash
gh auth login --hostname github.com    # interactive: pick "personal" account
gh auth login --hostname github.com    # interactive: pick "work" account
gh auth status                          # shows both, marks one as Active
gh auth switch -u work-account          # set the active account
```

Then `github-issue-agent-setup` from the repo dir picks up the active account automatically. **Each repo gets its own config file** at `~/.config/github-issue-agent/repos/<owner>__<name>.json`, so a personal repo's config never collides with a work repo's, and the agent will use the right account for whichever repo you're sitting in.

### Wizard validation

Before persisting anything, the wizard:

1. Verifies the cwd is inside a git repo (`git rev-parse --show-toplevel`)
2. Auto-detects `owner/name` from `git remote get-url origin`
3. Verifies `gh` is installed and authenticated
4. Probes `gh repo view <owner>/<name>` to confirm the active account can actually see the repo

If the active account can't see the repo (e.g. you logged in as `personal` but the repo belongs to `work`), the wizard prints both fixes and exits without writing anything:

```
Active gh account 'personal' cannot see 'work-org/internal-tool'.
Try one of:
  gh auth switch -u work-account
  gh auth login --hostname github.com
```

### Safety

- Config is written to `~/.config/github-issue-agent/repos/<owner>__<name>.json` with `chmod 600`. There's no token in the file (gh owns it) but the permission is enforced defensively in case a future field needs protection.
- The token redaction filter stays installed on the root logger as defense-in-depth: if any subsystem (gh stderr, a misconfigured logger) ever echoes a token-shaped substring, the regex safety net catches it before the line reaches a handler.
- Manual config (CI / pre-seed) is supported — see schema below. Drop one file per repo.

```json
{
  "label": "ai-fix",
  "mode": "semi",
  "poll_interval_min": 30,
  "test_command": "pytest -q",
  "lint_command": "ruff check ."
}
```

Save as `~/.config/github-issue-agent/repos/<owner>__<name>.json`, then `chmod 600` it.

---

## Security

| Concern | Defense |
|---|---|
| Token at rest | **The agent never holds a token.** `gh` CLI owns the credential (OS keychain or its own config tree). The agent's config files contain only label/mode/interval/command settings. |
| Token in logs | `logging.Filter` defensively regex-matches every GitHub token prefix (`ghp_`, `github_pat_`, `ghs_`, `gho_`, `ghu_`, `ghr_`) before the formatter runs — so even if `gh` stderr ever leaks a token-shaped substring, it gets scrubbed. |
| Per-repo config at rest | `~/.config/github-issue-agent/repos/<owner>__<name>.json` enforced at `chmod 600`. |
| Path traversal | Every fs tool resolves paths inside `repo_root`. `..`, absolute escape, and symlink escape all reject with `PATH_TRAVERSAL`. |
| Partial writes | `ghia.atomic.atomic_write_text` writes to `tempfile` → `os.fsync` → `os.replace`. Crash mid-write leaves the original intact. |
| Arbitrary shell in `run_tests` / `check_linting` | Allow-list-validated. Shell metacharacters (`&`, `|`, `;`, `$`, backticks, redirection) are rejected at wizard time AND at execution time. No `shell=True` anywhere. |
| Docker test escape | Container mounts repo **read-only** except a dedicated output volume; runs as `nobody` user; no host network; auto-removes on exit. |
| Direct push to default branch | `commit_changes` and `push_branch` refuse if HEAD is on the dynamically-detected default branch (`gh repo view` → `git symbolic-ref refs/remotes/origin/HEAD` → `git rev-parse --abbrev-ref @{upstream}` cascade). |
| Accidental merge | Full-auto mode opens PRs with `draft=true`. No merge tool is exposed by the MCP server. |
| Undo on someone else's commit | `undo_last_change` reads the HEAD commit's `author.email`, compares with `git config user.email`, and refuses with `UNDO_REFUSED_NOT_OURS` if they differ. Refuses with `UNDO_REFUSED_PROTECTED_BRANCH` if HEAD is the default branch. |
| Duplicate work | `check_issue_has_open_pr` scans open PRs for `Closes #N` / `Fixes #N` markers AND checks for local branches matching `fix/issue-{n}-*` before picking. |

---

## Troubleshooting

**`{success: false, code: "CONFIG_MISSING"}`**
`~/.config/github-issue-agent/repos/<owner>__<name>.json` is missing or unreadable for this repo.
Fix: `cd` into the repo, then `github-issue-agent-setup` (the console script installed by `bash install.sh`).

**`{success: false, code: "TOKEN_INVALID"}`**
`gh` is not authenticated, or the active account's credential expired.
Fix: `gh auth status` to see what gh thinks; `gh auth login --hostname github.com` to re-auth, or `gh auth switch -u <other>` if you need a different account.

**`{success: false, code: "REPO_NOT_FOUND"}`**
The active `gh` account doesn't have access to the repo `git remote get-url origin` points at.
Fix: `gh auth switch -u <account-with-access>` if you have the right credentials under another login, or ask the repo owner to grant your active account access.

**`{success: false, code: "INVALID_INPUT"}` mentioning "not inside a git repository" or "no 'origin' remote"**
Claude Code was launched from a directory that isn't a git repo (or has no `origin` remote).
Fix: `cd` into a git repo with an `origin` remote on github.com and re-run.

**`{success: false, code: "RATE_LIMITED"}`**
You've hit GitHub's REST rate limit (5,000/hr authenticated, 60/hr unauthenticated).
Fix: `ghia.network` already honours `Retry-After`; wait for the window to reset (`gh api rate_limit` shows when). For chronic offenders, lower the polling frequency in the wizard.

**`{success: false, code: "DOCKER_UNAVAILABLE"}`**
Docker daemon is not running or socket is not reachable.
Fix: start Docker (`sudo systemctl start docker` on Linux, Docker Desktop on macOS). The agent will continue without `run_tests` if you skip past it in semi mode, but full-auto requires it.

**`{success: false, code: "GIT_NOT_FOUND"}`**
`git` binary is not on `$PATH`.
Fix: install git (`sudo apt install git` / `brew install git`). The agent uses native git CLI throughout — no GitPython at runtime.

**`{success: false, code: "ON_DEFAULT_BRANCH_REFUSED"}`** (returned by `commit_changes` / `push_branch`)
HEAD is on the default branch and the protocol forbids direct pushes.
Fix: `git create_branch` to a `fix/issue-{n}-{slug}` branch first. The protocol always does this — you should only see this if you're calling tools manually.

**Picker page loads but issue list is blank**
Either the configured label has no open issues, or the GitHub fetch failed silently.
Fix: check `/mcp__github-issue-agent__status` for the last-fetch timestamp and any error code; force a refresh with `/mcp__github-issue-agent__fetch_now`; verify the label exists on the repo (case-sensitive).

**Polling timer is not firing**
The polling task only runs while the agent is `started`. Stopping cancels it.
Fix: `/mcp__github-issue-agent__status` — if `state` is `idle`, run `/mcp__github-issue-agent__start`. If polling is running but no fetches happen, check the configured `poll_interval_min` (minimum 5).

**`session.json.bak-{timestamp}` appears in `state/`**
Your session file got corrupted (typically a SIGKILL mid-write or a disk issue) and was auto-rotated so the agent could start cleanly. Safe to delete once you've confirmed nothing was in-flight.

**`claude mcp add github-issue-agent` fails or the slash command is missing in other repos**
Either the registration didn't run (claude CLI wasn't on PATH at install time) or it landed at the default `local` scope, which only works inside one project dir.
Fix: re-register at user scope so every project sees it. Point the interpreter at the agent's venv python:
`claude mcp add -s user github-issue-agent -- /full/path/to/.venv/bin/python -m server`

**Logs contain `***REDACTED***`**
That's the token redaction filter doing its job. If you ever see an unredacted token in stdout, an error message, or a file on disk, **that's a bug** — please file a security issue.

---

## Architecture

Single Python process, single asyncio event loop. FastMCP hosts the tool API over stdio to Claude Code. A Starlette sub-app serves the picker UI on `127.0.0.1:4242`. Subprocess calls (`git`, `docker`, `gh`) are offloaded via `asyncio.to_thread` so they never block the event loop. `SessionStore` is the **single writer** of persistent state, guarded by an `asyncio.Lock` — reads are snapshot-based and lock-free. Writes go through `ghia.atomic.atomic_write_text`. Every tool returns a structured `ToolResponse` — never raw dicts, never uncaught exceptions. Full rationale: TRD §1.3 and §2.

```
github-issue-agent/
├── server.py                    FastMCP entrypoint (TRD-011)
├── setup_wizard.py              Interactive bulk-collect wizard (TRD-010)
├── install.sh                   One-command installer (TRD-007)
├── pyproject.toml               Packaging; Python >= 3.10
├── requirements.txt             Pinned runtime + dev deps
├── ghia/                        Internal package
│   ├── app.py                   Composition root (GhiaApp dataclass)
│   ├── errors.py                ErrorCode enum + ToolResponse + @wrap_tool
│   ├── redaction.py             Token redaction logging filter
│   ├── atomic.py                Atomic write utilities
│   ├── paths.py                 Path-traversal guard
│   ├── config.py                Per-repo config model + load/save (chmod 600)
│   ├── repo_detect.py           Auto-detect owner/name from `git remote get-url origin`
│   ├── session.py               SessionStore with asyncio.Lock
│   ├── protocol.py              agent_protocol.md renderer
│   ├── convention_scan.py       CLAUDE.md / CONTRIBUTING.md discovery
│   ├── detection.py             Test-runner / linter auto-detection
│   ├── network.py               Rate-limit / transport-error helpers
│   ├── retry.py                 Generic retry policy (exp backoff + jitter)
│   ├── naming.py                Branch / commit / PR slug builder
│   ├── polling.py               Background polling task (start/stop)
│   ├── queue_processor.py       Serial issue-queue runner
│   ├── tools/
│   │   ├── control.py           start/stop/status/set_mode/fetch_now
│   │   ├── issues.py            list/get/pick/skip/post-comment/dup-check
│   │   ├── fs.py                read_file/write_file/list_directory/...
│   │   ├── git.py               create_branch/commit_changes/push_branch/...
│   │   ├── pr.py                create_pr (via gh CLI)
│   │   ├── lint.py              check_linting (allow-listed)
│   │   ├── tests.py             run_tests (Docker sandbox)
│   │   ├── undo.py              undo_last_change (author-email guarded)
│   │   └── validation.py        Command allow-list
│   ├── ui/
│   │   ├── server.py            Starlette sub-app on 127.0.0.1:4242
│   │   ├── terminal.py          rich fallback for headless / SSH
│   │   └── opener.py            Headless detection + browser orchestrator
│   └── integrations/
│       ├── gh_cli.py            Async wrapper over the `gh` CLI subprocess
│       └── docker_runner.py     Docker SDK wrapper for run_tests
│   ├── ui_static/picker.html    Self-contained picker UI (ships in wheel)
│   └── prompts/agent_protocol.md  Injected into Claude on start (ships in wheel)
├── tests/                       pytest + pytest-asyncio (479 tests)
└── docs/
    ├── PRD/PRD-2026-001-…       Product requirements (28 REQs, 85 ACs)
    └── TRD/TRD-2026-001-…       Technical design (76 TRD tasks)
```

### Run the test suite

```bash
python -m pytest -q
```

479 tests, ~19s runtime. Coverage gate ≥ 80%.

```bash
python -m pytest tests/test_redaction.py -v
```

Run a single module while iterating.

### Smoke tests

```bash
python -c "from ghia.app import create_app; print('ok')"
```

Verifies the composition root imports.

```bash
python -c "import ghia, ghia.tools.git, ghia.tools.fs, ghia.tools.pr, ghia.tools.lint, ghia.tools.tests, ghia.tools.undo, ghia.tools.issues, ghia.tools.control, ghia.queue_processor, ghia.polling, ghia.network, ghia.naming, ghia.retry, ghia.repo_detect, ghia.integrations.gh_cli, ghia.integrations.docker_runner, ghia.ui.server, ghia.ui.terminal, ghia.ui.opener; print('OK')"
```

Verifies every runtime module loads cleanly.

---

## Roadmap

- **v0.1** — Foundation + Core Control primitives (Sprint 1, shipped)
- **v0.2** — Bulk setup wizard + GitHub client (Sprint 2, shipped)
- **v0.3** — Browser picker + terminal fallback + duplicate-PR detection (Sprint 3, shipped)
- **v0.4** — Full code/git/PR/test/undo toolkit (Sprint 4, shipped)
- **v1.0** — Polling, serial queue, retry policy, packaging, polished docs (Sprint 5, shipped)

Post-MVP, explicitly out of scope for v1: multi-repo support, GitLab / Bitbucket, PR review agent, Slack / Discord notifications, learning loop from merged vs rejected PRs, CI integration. Windows users (non-WSL) are also out of scope this release.

---

## Contributing

Not open for external contributions yet — still mid-build. Once MVP ships:

1. Fork + feature branch (`fix/issue-{n}-{slug}` convention)
2. `python -m pytest` must pass with coverage ≥ 80%
3. Changes require at least one paired `TEST` task
4. Conventional-commit message format (see recent `git log` for style)

---

## License

TBD — placeholder until first public release.
