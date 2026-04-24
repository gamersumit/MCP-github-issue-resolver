# github-issue-agent

**MCP server that lets Claude Code resolve GitHub issues end-to-end — from backlog to open PR.**

Drops into Claude Code as a local Model Context Protocol (MCP) server, stays idle until you ask it to start, opens a browser picker for selecting issues, and either walks you through each fix (semi-auto) or drives PRs to completion autonomously (full-auto).

---

## ⚠️ Status — Work in Progress

| Sprint | Scope | Status |
|---|---|---|
| Sprint 1a — Foundation (TRD-001..006) | error types, token redaction, atomic writes, path guards, config, session state | ✅ **Shipped** |
| Sprint 1b — Core Control (TRD-011..014, TRD-033) | MCP bootstrap, control tools, protocol renderer, convention discovery | ✅ **Shipped** |
| Sprint 2 — Setup wizard + GitHub client | install.sh, wizard, token validation, issue fetching | ✅ **Shipped — wizard, token validation, auto-detection landed.** |
| Sprint 3 — Picker UI + duplicate detection | browser UI, terminal fallback, duplicate-PR checks | ✅ **Shipped — Starlette picker on `127.0.0.1:4242`, `rich` terminal fallback, headless auto-detection.** |
| Sprint 4 — Code & Git ops | filesystem tools, git CLI wrapper, PR creation, Docker-sandboxed tests, undo | ⏳ Pending |
| Sprint 5 — Polish & ship | serial queue, polling timer, README polish, packaging | ⏳ Pending |

**What runs today:** `ghia/` package (foundation + control tools + protocol renderer + setup wizard + token validator + test/lint auto-detection + command allow-list + GitHub client + issue tools + browser/terminal picker UI), tested with 282 passing tests. `server.py` registers with FastMCP, `/issue-agent start/stop/status/set_mode/fetch_now` are callable. The picker UI lives at `ui_static/picker.html` and is served by a Starlette sub-app bound to `127.0.0.1:4242`; headless environments transparently fall back to a `rich` terminal table. Installation is a one-command `bash install.sh` that creates a venv, installs deps, runs the interactive wizard, and registers the MCP server with Claude Code. Code-writing / PR creation are still stubs until Cluster 5.

See `docs/PRD/PRD-2026-001-github-issue-agent.md` and `docs/TRD/TRD-2026-001-github-issue-agent.md` for the full specification and technical design.

---

## Concept

Two operating modes:

- **SEMI-AUTO** — Claude proposes each step, you approve before it acts (default, safe for sensitive repos)
- **FULL-AUTO** — Claude executes end-to-end and opens **draft** PRs for your review (never auto-merges)

You only need to know two commands once it ships:

```
/issue-agent start     # opens issue picker, loads workflow protocol
/issue-agent stop      # pauses the agent, keeps session history
```

Everything else (reading files, writing fixes, running tests, creating branches, opening PRs) is handled through MCP tools the agent protocol drives automatically.

---

## Prerequisites

- **Python 3.10+**
- **Git** on `$PATH` (default branch is auto-detected — no `main` / `master` hardcoding)
- **Claude Code** CLI (for MCP registration) — [install guide](https://docs.claude.com/en/docs/claude-code)
- **Docker** (for test sandboxing; not required for current Sprint 1 functionality — will be required once Sprint 4 lands)
- **`gh` CLI** (optional but recommended for PR creation; PyGithub fallback is used if absent)

## Install

One command — creates a venv, installs deps, runs the interactive setup wizard, registers with Claude Code:

```bash
git clone <this-repo> github-issue-agent
cd github-issue-agent
bash install.sh
```

The installer is idempotent — re-running it upgrades dependencies and re-enters the wizard with your current config values pre-filled as defaults (ENTER accepts each one, type a new value to change it).

If you only want to re-run the wizard (for example to change your token or swap the target repo), activate the venv and invoke it directly:

```bash
source .venv/bin/activate
python -m setup_wizard
```

### What the wizard asks

- **GitHub token** — masked input. A [fine-grained PAT](https://github.com/settings/personal-access-tokens/new) scoped to a single repo (Issues: R/W, Pull requests: R/W, Contents: R/W) is recommended; classic PATs with `repo` scope also work. The wizard probes `GET /user` to validate the token before persisting anything.
- **Target repo** in `owner/name` form. The wizard probes `GET /repos/{owner}/{name}` to confirm the token can actually see the repo.
- **Label** used to tag issues the agent should pick up (default: `ai-fix`).
- **Mode** — `semi` (approves each step, default) or `full` (runs end-to-end and opens draft PRs).
- **Poll interval** — how often the agent checks for new labelled issues (default: 30 min, minimum: 5 min).
- **Test command** and **lint command** — auto-detected from `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, `Gemfile`, `pom.xml`, `build.gradle`, `mix.exs` (and more). You can ENTER to accept the detection or type a custom command. Commands are allow-list-validated — shell metacharacters (`&`, `|`, `;`, `$`, backticks) are rejected.

Config is persisted to `~/.config/github-issue-agent/config.json` with `chmod 600`. The token is never printed back to the terminal, logged, or echoed in error messages — `ghia.redaction` scrubs it from every log record with literal-replace + regex safety net.

### Manual config (advanced / offline)

If you need to pre-seed a config without running the wizard (e.g. CI provisioning), the expected schema is:

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

Then `chmod 600 ~/.config/github-issue-agent/config.json` — the loader enforces this permission on writes and verifies on load.

**Token**: use a [fine-grained personal access token](https://github.com/settings/personal-access-tokens/new) scoped to a single repo with `issues: read+write`, `pull_requests: read+write`, `contents: read+write`. Classic PATs (`ghp_...`) also work. The token is never logged, printed, or included in error messages — `ghia.redaction` filters it out of every log line with both literal-replace and regex defense (covers all documented GitHub token prefixes).

---

## MCP tools available today

Once registered, these tools appear in Claude Code's tool list:

| Tool | Status | What it does |
|---|---|---|
| `issue_agent_start` | Partial | Activates agent, runs Step 0 (reads `CLAUDE.md` / `CONTRIBUTING.md` / `AGENTS.md` / `.cursor/rules/*.md` / `.editorconfig` / `README.md` for convention summary), injects workflow protocol. Issue-fetching stub — returns current queue only. |
| `issue_agent_stop` | Complete | Pauses agent, preserves completed/skipped counts + discovered conventions across sessions. |
| `issue_agent_status` | Complete | Returns full SessionState + human-readable status line. |
| `issue_agent_set_mode` | Complete | Switches between `"semi"` and `"full"`. Takes effect **immediately** at the next decision point — in-flight work is preserved. |
| `issue_agent_fetch_now` | Stubbed | Will trigger an on-demand issue fetch once Cluster 4 (GitHub client) lands. |

Tools pending from later sprints: `list_issues`, `get_issue`, `pick_issues`, `skip_issue`, `post_issue_comment`, `check_issue_has_open_pr`, `read_file`, `write_file`, `list_directory`, `search_codebase`, `get_repo_structure`, `read_multiple_files`, `create_branch`, `git_diff`, `commit_changes`, `push_branch`, `get_current_branch`, `get_default_branch`, `create_pr`, `run_tests`, `check_linting`, `undo_last_change`.

---

## Development

### Run the test suite

```bash
python -m pytest tests/ -v
```

282 tests today, < 7 seconds runtime. Coverage target is ≥ 80% (enforced at sprint exit).

### Run a single test module

```bash
python -m pytest tests/test_redaction.py -v
```

### Dev loop

```bash
# smoke-test imports
python -c "from ghia.app import create_app; print('ok')"

# quick token-redaction sanity check
python -c "
import logging
from ghia.redaction import install_filter, set_token
install_filter()
set_token('ghp_1234567890abcdefghijklmnopqrstuvwxyz')
logging.warning('token in log: ghp_1234567890abcdefghijklmnopqrstuvwxyz')
# -> logged as: 'token in log: ***REDACTED***'
"
```

### Project layout

```
github-issue-agent/
├── server.py                 ← FastMCP entrypoint (TRD-011)
├── setup_wizard.py           ← interactive bulk-collect wizard (TRD-010)
├── install.sh                ← one-command installer (TRD-007)
├── pyproject.toml            ← packaging; Python >= 3.10
├── requirements.txt          ← pinned deps
├── ghia/                     ← internal package
│   ├── app.py                ← composition root (GhiaApp dataclass)
│   ├── errors.py             ← ErrorCode enum + ToolResponse + @wrap_tool
│   ├── redaction.py          ← token redaction logging filter
│   ├── atomic.py             ← atomic write utilities
│   ├── paths.py              ← path-traversal guard
│   ├── config.py             ← Config model + load/save (chmod 600)
│   ├── session.py            ← SessionStore with asyncio.Lock
│   ├── protocol.py           ← agent_protocol.md renderer
│   ├── convention_scan.py    ← CLAUDE.md / CONTRIBUTING.md discovery
│   ├── detection.py          ← test-runner / linter auto-detection (TRD-009)
│   ├── github_client_light.py← minimal httpx client for setup-time probes (TRD-008)
│   ├── tools/
│   │   ├── control.py        ← start/stop/status/set_mode/fetch_now
│   │   ├── issues.py         ← list/get/pick/skip/post-comment/dup-check (TRD-016/017)
│   │   └── validation.py     ← command allow-list (TRD-008, AC-017-3)
│   └── ui/                   ← picker subsystem (TRD-018..021)
│       ├── server.py         ← Starlette sub-app on 127.0.0.1:4242
│       ├── terminal.py       ← `rich` fallback for headless / SSH
│       └── opener.py         ← headless detection + browser orchestrator
├── ui_static/
│   └── picker.html           ← self-contained picker UI (no CDN deps)
├── prompts/
│   └── agent_protocol.md     ← injected into Claude on start
├── tests/                    ← pytest + pytest-asyncio (201 tests)
└── docs/
    ├── PRD/PRD-2026-001-…    ← product requirements (28 REQs, 85 ACs)
    └── TRD/TRD-2026-001-…    ← technical design (76 TRD tasks)
```

### Architecture in one paragraph

Single Python process, asyncio event loop. FastMCP hosts the tool API over stdio to Claude Code. A Starlette sub-app (Sprint 3) will serve the picker UI on `localhost:4242`. Subprocess calls (`git`, `docker`, `gh`) are offloaded via `asyncio.to_thread` so they never block the event loop. `SessionStore` is the **single writer** of persistent state, guarded by an `asyncio.Lock` — reads are snapshot-based and lock-free. Writes go through `ghia.atomic.atomic_write_text` (write-temp → fsync → rename). Every tool returns a structured `ToolResponse` — never raw dicts, never uncaught exceptions. Full rationale: see TRD §1.3 and §2.

---

## Security model

| Concern | Defense |
|---|---|
| Token in logs | `logging.Filter` subclass replaces the live token literal AND regex-matches all GitHub token prefixes (`ghp_`, `github_pat_`, `ghs_`, `gho_`, `ghu_`, `ghr_`) |
| Token at rest | `~/.config/github-issue-agent/config.json` with `chmod 600`; permission verified at load |
| Path traversal | All filesystem tools resolve paths inside the repo root; `..`, absolute escape, symlink escape all rejected with `PATH_TRAVERSAL` error |
| Partial writes | Atomic writes via temp file + `os.replace`; fsync before rename on POSIX |
| Arbitrary shell | `run_tests` and `check_linting` will allow-list-validate commands (Sprint 4) — no free-form shell accepted |
| Docker escape | Test container mounts repo **read-only** except a dedicated output volume; runs as `nobody` user (Sprint 4) |
| Direct push to main | Every issue works on a new `fix/issue-{n}-{slug}` branch; `commit_changes`/`push_branch` refuse if HEAD is on the dynamically-detected default branch (Sprint 4) |
| Accidental merge | Full-auto mode opens PRs with `draft=true`; no merge tool is exposed (Sprint 4) |

---

## Agent protocol (the workflow Claude follows)

Stored at `prompts/agent_protocol.md` and injected into Claude's context when `issue_agent_start` runs. It documents the exact step sequence for both semi and full auto modes, plus the non-negotiable rules (never push to default branch, always include `Closes #N` in PR body, always lint before test, max 3 retries per issue in full-auto, etc.).

The template uses:
- `{var}` placeholders for render-time substitution (repo, mode, timestamp, queue, conventions)
- `{{% if mode == "semi" %}}...{{% endif %}}` blocks for mode-specific sections

---

## Troubleshooting

**`claude mcp add github-issue-agent` fails**
Make sure you've activated the venv (`source .venv/bin/activate`) so `python -m server` resolves to the right interpreter. Alternatively register with an absolute path: `claude mcp add github-issue-agent -- /full/path/to/.venv/bin/python -m server`.

**Tools return `{success: false, code: "CONFIG_MISSING"}`**
Your `~/.config/github-issue-agent/config.json` is missing or unreadable. Re-run the setup wizard: `source .venv/bin/activate && python -m setup_wizard`.

**Tools return `{success: false, code: "TOKEN_INVALID"}`**
Token is expired, revoked, or missing required scopes. Regenerate from [github.com/settings/tokens](https://github.com/settings/tokens) or [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new) and re-run `python -m setup_wizard` to update the stored token.

**`session.json.bak-{ts}` appears in `state/`**
Your session file got corrupted (unusual — typically means a disk issue or SIGKILL mid-write) and was auto-rotated to a backup so the agent could start cleanly. The backup is safe to delete once you've confirmed nothing important was in-flight.

**Logs contain `***REDACTED***` — is that right?**
Yes. That's the token redaction filter doing its job. If you see an unredacted token anywhere (stdout, error message, file on disk), **that's a bug** — please file it as a security issue.

---

## Contributing

Not open for external contributions yet — still mid-build. Once MVP ships:

1. Fork + feature branch (`fix/issue-{n}-{slug}` convention)
2. `python -m pytest` must pass with coverage ≥ 80%
3. Changes require at least one paired `TEST` task
4. Conventional-commit message format (see recent `git log` for style)

---

## Roadmap

- **v0.1** (current) — Foundation + Core Control primitives, test-covered
- **v0.2** (Sprint 2) — Bulk setup wizard, GitHub token validation, issue listing
- **v0.3** (Sprint 3) — Browser picker UI + terminal fallback + duplicate-PR detection
- **v0.4** (Sprint 4) — Full code/git/test toolkit; real end-to-end fix flow
- **v1.0** (Sprint 5) — Polling, serial queue orchestration, one-command installer, polished docs

Post-MVP ideas (explicitly out of scope for v1): multi-repo support, GitLab/Bitbucket, PR review agent, Slack notifications, learning loop from merged vs rejected PRs, CI integration.

---

## License

TBD — placeholder until first public release.
