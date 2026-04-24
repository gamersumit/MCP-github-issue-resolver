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

**453 tests** pass in ~19s. Coverage is enforced ≥ 80% at sprint exit. All MCP tools listed below are wired and reachable from Claude Code.

See `docs/PRD/PRD-2026-001-github-issue-agent.md` and `docs/TRD/TRD-2026-001-github-issue-agent.md` for the full specification and technical design.

---

## Install

```bash
git clone <this-repo> github-issue-agent
cd github-issue-agent
bash install.sh
```

That's it. The installer is idempotent — re-running it upgrades dependencies and re-enters the wizard with your current config values pre-filled (ENTER accepts each one, type a new value to change it).

### Prerequisites

- **Python 3.10+** (stdlib `tomllib` is auto-used on 3.11+, `tomli` backport pulled in for 3.10)
- **Git** on `$PATH` — default branch is auto-detected per repo (no `main` / `master` hardcoding)
- **Claude Code** CLI — [install guide](https://docs.claude.com/en/docs/claude-code) — for MCP registration
- **Docker** — optional but required for `run_tests` (sandboxed test execution); `run_tests` returns `DOCKER_UNAVAILABLE` with install hint when the daemon is unreachable
- **`gh` CLI** — optional; `create_pr` falls back to PyGithub when `gh` is absent

### What the installer does

1. Verifies Python ≥ 3.10
2. Creates `./.venv` (or reuses an existing one)
3. `pip install -r requirements.txt` and `pip install -e .` (editable so source edits take effect immediately)
4. Runs `python -m setup_wizard` interactively — collects token, repo, label, mode, poll interval, test/lint commands
5. Best-effort `claude mcp add github-issue-agent -- $VENV/bin/python -m server` so the `/issue-agent ...` slash commands appear in Claude Code

If `claude` is not on `$PATH`, the installer prints the exact registration command you can run later.

---

## Quick Start

After `bash install.sh` completes, open Claude Code and type:

```
/issue-agent start
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
/issue-agent stop
```

This pauses the agent — the queue, completed/skipped counters, and conventions summary all survive across `start` calls.

---

## Commands

### MCP tools (registered with FastMCP, callable from Claude Code)

| Slash command | Tool name | What it does |
|---|---|---|
| `/issue-agent start` | `issue_agent_start` | Activates agent, runs Step 0 (convention discovery), injects workflow protocol, kicks off polling timer. |
| `/issue-agent stop` | `issue_agent_stop` | Pauses agent, cancels polling task, preserves session state. |
| `/issue-agent status` | `issue_agent_status` | Returns full `SessionState` + human-readable summary (mode, queue depth, last-fetch timestamp). |
| `/issue-agent set_mode` | `issue_agent_set_mode` | Switches between `"semi"` and `"full"`. Takes effect **immediately** at the next decision point — in-flight work is preserved. |
| `/issue-agent fetch_now` | `issue_agent_fetch_now` | Force an immediate issue refresh, bypassing the poll interval. |

Internal tools the protocol drives (Claude calls these automatically — you don't type them):

`list_issues`, `get_issue`, `pick_issue`, `skip_issue`, `post_issue_comment`, `check_issue_has_open_pr`, `read_file`, `write_file`, `list_directory`, `search_codebase`, `get_repo_structure`, `read_multiple_files`, `create_branch`, `git_diff`, `commit_changes`, `push_branch`, `get_current_branch`, `get_default_branch`, `create_pr`, `run_tests`, `check_linting`, `undo_last_change`.

### Setup wizard

```bash
source .venv/bin/activate
python -m setup_wizard
```

Run any time you want to change your token, swap repos, change the label, switch default mode, adjust the poll interval, or override the auto-detected test/lint commands. The wizard reads your current config and pre-fills every prompt — ENTER accepts the existing value.

---

## Modes

You can be in exactly one mode at a time. Switch mid-session with `/issue-agent set_mode semi` or `/issue-agent set_mode full`.

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

A [**fine-grained personal access token**](https://github.com/settings/personal-access-tokens/new) scoped to a single repo is recommended. Required permissions:

- **Issues** — Read & Write (list, comment)
- **Pull requests** — Read & Write (create, check duplicates)
- **Contents** — Read & Write (push branches)

Classic PATs (`ghp_...`) with the `repo` scope also work; the wizard accepts both.

The wizard validates the token before persisting:

1. `GET /user` — confirms the token is live
2. `GET /repos/{owner}/{name}` — confirms the token can see the configured repo

On either failure, you get a structured error (`TOKEN_INVALID` or `REPO_NOT_FOUND`) and nothing is written to disk.

### Safety

- Config is written to `~/.config/github-issue-agent/config.json` with `chmod 600`. Permission is verified on every load.
- The token is **never** printed back to the terminal, written to a log line, or included in an error message. `ghia.redaction` installs a `logging.Filter` on the root logger that:
  - replaces the live token literal with `***REDACTED***`
  - regex-matches every documented GitHub prefix (`ghp_`, `github_pat_`, `ghs_`, `gho_`, `ghu_`, `ghr_`) as a defense-in-depth net for tokens that aren't the configured one
- Manual config (CI / pre-seed) is supported — see schema below.

```json
{
  "token": "github_pat_...",
  "repo": "your-owner/your-repo",
  "label": "ai-fix",
  "mode": "semi",
  "poll_interval_min": 30,
  "test_command": "pytest -q",
  "lint_command": "ruff check ."
}
```

Then `chmod 600 ~/.config/github-issue-agent/config.json`.

---

## Security

| Concern | Defense |
|---|---|
| Token in logs | `logging.Filter` replaces the live token literal AND regex-matches all GitHub token prefixes (`ghp_`, `github_pat_`, `ghs_`, `gho_`, `ghu_`, `ghr_`) before the formatter runs. |
| Token at rest | `~/.config/github-issue-agent/config.json` enforced at `chmod 600`. Permission verified on load — wider perms refuse to start. |
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
`~/.config/github-issue-agent/config.json` is missing or unreadable.
Fix: `source .venv/bin/activate && python -m setup_wizard`

**`{success: false, code: "TOKEN_INVALID"}`**
Token is expired, revoked, or missing required scopes.
Fix: regenerate at [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new) (or [github.com/settings/tokens](https://github.com/settings/tokens) for classic), then re-run `python -m setup_wizard`.

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
Fix: check `/issue-agent status` for the last-fetch timestamp and any error code; force a refresh with `/issue-agent fetch_now`; verify the label exists on the repo (case-sensitive).

**Polling timer is not firing**
The polling task only runs while the agent is `started`. Stopping cancels it.
Fix: `/issue-agent status` — if `state` is `idle`, run `/issue-agent start`. If polling is running but no fetches happen, check the configured `poll_interval_min` (minimum 5).

**`session.json.bak-{timestamp}` appears in `state/`**
Your session file got corrupted (typically a SIGKILL mid-write or a disk issue) and was auto-rotated so the agent could start cleanly. Safe to delete once you've confirmed nothing was in-flight.

**`claude mcp add github-issue-agent` fails**
The interpreter path needs to point at the venv's python.
Fix: `claude mcp add github-issue-agent -- /full/path/to/.venv/bin/python -m server`

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
│   ├── config.py                Config model + load/save (chmod 600)
│   ├── session.py               SessionStore with asyncio.Lock
│   ├── protocol.py              agent_protocol.md renderer
│   ├── convention_scan.py       CLAUDE.md / CONTRIBUTING.md discovery
│   ├── detection.py             Test-runner / linter auto-detection
│   ├── github_client_light.py   Minimal httpx client for setup-time probes
│   ├── network.py               Retry-After honouring HTTP retry helper
│   ├── retry.py                 Generic retry policy (exp backoff + jitter)
│   ├── naming.py                Branch / commit / PR slug builder
│   ├── polling.py               Background polling task (start/stop)
│   ├── queue_processor.py       Serial issue-queue runner
│   ├── tools/
│   │   ├── control.py           start/stop/status/set_mode/fetch_now
│   │   ├── issues.py            list/get/pick/skip/post-comment/dup-check
│   │   ├── fs.py                read_file/write_file/list_directory/...
│   │   ├── git.py               create_branch/commit_changes/push_branch/...
│   │   ├── pr.py                create_pr (gh-cli with PyGithub fallback)
│   │   ├── lint.py              check_linting (allow-listed)
│   │   ├── tests.py             run_tests (Docker sandbox)
│   │   ├── undo.py              undo_last_change (author-email guarded)
│   │   └── validation.py        Command allow-list
│   ├── ui/
│   │   ├── server.py            Starlette sub-app on 127.0.0.1:4242
│   │   ├── terminal.py          rich fallback for headless / SSH
│   │   └── opener.py            Headless detection + browser orchestrator
│   └── integrations/
│       ├── github.py            Full PyGithub wrapper (GitHubClient)
│       └── docker_runner.py     Docker SDK wrapper for run_tests
│   ├── ui_static/picker.html    Self-contained picker UI (ships in wheel)
│   └── prompts/agent_protocol.md  Injected into Claude on start (ships in wheel)
├── tests/                       pytest + pytest-asyncio (453 tests)
└── docs/
    ├── PRD/PRD-2026-001-…       Product requirements (28 REQs, 85 ACs)
    └── TRD/TRD-2026-001-…       Technical design (76 TRD tasks)
```

### Run the test suite

```bash
python -m pytest -q
```

453 tests, ~19s runtime. Coverage gate ≥ 80%.

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
python -c "import ghia, ghia.tools.git, ghia.tools.fs, ghia.tools.pr, ghia.tools.lint, ghia.tools.tests, ghia.tools.undo, ghia.tools.issues, ghia.tools.control, ghia.queue_processor, ghia.polling, ghia.network, ghia.naming, ghia.retry, ghia.integrations.github, ghia.integrations.docker_runner, ghia.ui.server, ghia.ui.terminal, ghia.ui.opener; print('OK')"
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
