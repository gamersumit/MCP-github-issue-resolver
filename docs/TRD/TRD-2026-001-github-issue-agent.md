---
Document ID: TRD-2026-001
PRD Reference: PRD-2026-001
Version: 1.0.0
Status: Draft
Date: 2026-04-23
Architecture Choice: Option C — Single-process async with clean module boundaries
Design Readiness Score: 4.13 / 5.0 (PASS)
Team Tier: COMPLEX (auto-configured 2026-04-23)
Total Implementation Tasks: 35 + 1 infra
Total Test Tasks: 35
Estimated Effort: ~164 hours (solo, ~5-6 weeks full-time)
---

# TRD-2026-001 — GitHub Issue Agent (Technical Design)

## 1. Architecture Decision

### 1.1 Chosen Approach
**Option C — Single-process async with clean module boundaries.**

One Python process hosts the FastMCP server, a Starlette sub-app for the picker UI, and an asyncio background task for polling — all sharing the same event loop. Blocking subprocess calls (`git`, `docker`, `gh`) are offloaded via `asyncio.to_thread`. State lives in an atomic-write JSON file guarded by an `asyncio.Lock`.

### 1.2 Alternatives Considered
| Option | Pros | Cons | Verdict |
|---|---|---|---|
| A — Monolithic sync | Simplest; fewest moving parts | Mixes sync & async; thread-based UI lifecycle is brittle | Rejected: FastMCP is async anyway |
| B — Multi-process | Robust isolation; polling survives MCP crashes | Install + IPC complexity; overkill for solo MVP | Rejected: premature scale |
| **C — Single-process async** | Single install; async-native; clean seams for future extraction to B | Requires disciplined async hygiene | **Chosen** |

### 1.3 Key Design Principles
1. **Idle by default, explicit activation** — no side effects before `issue_agent_start`.
2. **Every tool is a pure async function** returning `{success, data | error, code}`.
3. **SessionStore is the single writer of state**, guarded by `asyncio.Lock`; all other modules read-only.
4. **Subprocess calls never block the loop** — wrapped in `asyncio.to_thread`.
5. **Security primitives are composition-level, not per-tool**: redaction filter wraps logger, path guard wraps all fs tools, allow-list gates `run_tests` and `check_linting`.
6. **Safe-by-default in full-auto**: PRs are drafts, no merge tool exists, writes are atomic, `undo_last_change` refuses on protected branches.

---

## 2. System Architecture

### 2.1 Component Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                     Claude Code (host)                       │
│                            │                                 │
│                            │ MCP stdio (JSON-RPC)            │
│                            ▼                                 │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ github-issue-agent (1 process, asyncio event loop)     │  │
│  │                                                        │  │
│  │  ┌────────────┐   ┌────────────┐   ┌────────────────┐  │  │
│  │  │ FastMCP    │   │ Starlette  │   │ Polling Task   │  │  │
│  │  │ tools/*    │   │ UI :4242   │   │ (asyncio.Task) │  │  │
│  │  └─────┬──────┘   └─────┬──────┘   └────────┬───────┘  │  │
│  │        └────────┬───────┴───────────────────┘          │  │
│  │                 ▼                                      │  │
│  │        ┌────────────────┐                              │  │
│  │        │ SessionStore   │ ◄── asyncio.Lock + atomic   │  │
│  │        │  (singleton)   │     JSON writes              │  │
│  │        └────────┬───────┘                              │  │
│  │                 │                                      │  │
│  │   ┌─────────────┼──────────────┐                       │  │
│  │   ▼             ▼              ▼                       │  │
│  │ tools/        tools/         tools/                    │  │
│  │ issue_*       fs_*           git_*                     │  │
│  └────┬─────────────┬──────────────┬────────────────────────┘
└───────┼─────────────┼──────────────┼──────────────────────────┘
        ▼             ▼              ▼
   GitHub API    Filesystem    git CLI · gh CLI · Docker
   (PyGithub)    (atomic IO)    (subprocess + asyncio.to_thread)
```

### 2.2 Module Layout

```
github-issue-agent/
├── server.py                    # MCP entrypoint, FastMCP registration
├── setup.py                     # wizard
├── install.sh
├── requirements.txt
├── pyproject.toml
│
├── ghia/                        # internal package
│   ├── __init__.py
│   ├── app.py                   # asyncio composition root
│   ├── config.py                # config load/save + chmod
│   ├── session.py               # SessionStore singleton w/ asyncio.Lock
│   ├── errors.py                # structured error codes (enum)
│   ├── redaction.py             # token redaction filter
│   ├── protocol.py              # agent_protocol.md renderer
│   ├── polling.py               # asyncio polling task
│   │
│   ├── tools/
│   │   ├── control.py           # start/stop/status/set_mode/fetch_now
│   │   ├── issues.py            # list/get/pick/skip/comment/check_duplicate
│   │   ├── fs.py                # read/write/list/search/tree
│   │   ├── git.py               # branch/diff/commit/push/default_branch
│   │   ├── pr.py                # create_pr
│   │   ├── tests.py             # run_tests (Docker)
│   │   ├── lint.py              # check_linting
│   │   └── undo.py              # undo_last_change
│   │
│   ├── ui/
│   │   ├── server.py            # Starlette sub-app
│   │   ├── opener.py            # browser launcher w/ headless detection
│   │   └── terminal.py          # rich-based terminal picker
│   │
│   └── integrations/
│       ├── github.py            # PyGithub client + redaction hooks
│       ├── gh_cli.py            # thin `gh` wrapper
│       └── docker_runner.py     # Docker SDK wrapper + allow-list
│
├── prompts/agent_protocol.md    # workflow instructions template
├── ui_static/picker.html        # self-contained HTML
└── state/session.json           # runtime; gitignored
```

### 2.3 Data Flow: `/issue-agent start` (semi-auto)

1. Claude calls `issue_agent_start` (MCP tool over stdio).
2. `control.py:start` → SessionStore.lock.acquire, status = `active`.
3. `GitHubClient.validate_token()` → `GET /user`; on failure, release lock and return `TOKEN_INVALID`.
4. `issues.list_issues(label)` → PyGithub, result cached in-memory.
5. `ConventionDiscovery.run()` → reads CLAUDE.md, CONTRIBUTING.md, etc.; stores summary in SessionStore.
6. `ui.opener.try_browser()` → attempts to launch `localhost:4242`; on headless detect (`$DISPLAY` empty, no browser bin), falls back to `ui.terminal.pick()`.
7. User selects issues → POST `/api/confirm` → SessionStore.update(queue=..., mode=...).
8. `polling.start_task()` → asyncio.create_task, timer = config.poll_interval_min.
9. `protocol.render(mode, queue, conventions)` → returned string.
10. SessionStore.lock.release, return `{success: true, data: {protocol, queue, mode}}`.

### 2.4 Data Flow: Mode switch mid-issue (REQ-007, immediate)

1. User calls `issue_agent_set_mode("full")` while semi-mode issue #42 is mid-workflow.
2. `control.py:set_mode` acquires SessionStore.lock, writes `mode="full"`, releases.
3. The next tool call made by Claude (e.g., after writing code) reads `SessionStore.mode` at its decision point.
4. The protocol's checkpoint logic uses a helper `await should_prompt_user()` that reads current mode.
5. If mode flipped to `full`, `should_prompt_user()` returns `False` → the checkpoint is skipped.
6. No in-flight state is lost (branch, edits, commits preserved).

### 2.5 Concurrency Model
- **One writer**: SessionStore is the only code path that mutates state file. All mutations go through `async with session.lock:`.
- **Multiple readers**: any async function can `await session.read()` without lock (read is atomic: load JSON snapshot returned as immutable dict).
- **Polling isolation**: polling task writes to SessionStore under the lock like any other writer.
- **UI isolation**: UI server uses SessionStore read/write the same way; no separate cache.

### 2.6 Integration Points

| Point | Protocol | Data Format | Error Mode |
|---|---|---|---|
| Claude ↔ MCP | FastMCP stdio | `{success, data\|error, code}` | Never raise; always structured |
| MCP → GitHub API | HTTPS (PyGithub/httpx) | JSON | `TOKEN_INVALID`, `RATE_LIMITED`, `NETWORK_ERROR`, `REPO_NOT_FOUND` |
| MCP → git CLI | subprocess | text | `GIT_ERROR`, `GIT_NOT_FOUND`, `ON_DEFAULT_BRANCH_REFUSED` |
| MCP → gh CLI | subprocess | JSON (`gh --json`) | Falls back to PyGithub if `gh` absent |
| MCP → Docker | Docker SDK (unix socket) | Python objects | `DOCKER_UNAVAILABLE`, `TEST_FAILED` |
| MCP ↔ UI server | HTTP `localhost:4242` | JSON | Locked to 127.0.0.1 bind; no CORS needed |
| SessionStore → JSON file | atomic rename (temp → target) | JSON | Corrupted → rotate to `.bak-{ts}`, start fresh |

### 2.7 Technology Choices

| Choice | Rationale |
|---|---|
| FastMCP | PRD mandate; async stdio MCP |
| Starlette | Async-native, mounts cleanly into the MCP event loop |
| PyGithub + httpx | Mature typed wrapper; httpx for async paths |
| `gh` CLI preferred, PyGithub fallback | `gh` handles auth + matches user mental model; PyGithub for headless/CI |
| Native `git` CLI via subprocess | Claude runs in repo; avoids GitPython quirks with submodules |
| Docker SDK for Python | Better structured results than `docker` CLI shell-out |
| `rich` | Spec mandate; terminal picker + wizard |
| Pydantic v2 | Spec mandate; tool-boundary input validation |
| `asyncio.to_thread` | Standard pattern for blocking subprocess; no extra deps |
| pytest + pytest-asyncio | De-facto standard for async Python tests |
| Playwright (light) for UI tests | Real browser keyboard/click simulation for `picker.html` |

---

## 3. Master Task List

> Task format: `TRD-NNN: {description} [Nh] [satisfies REQ-NNN(-X)...]`
> Every user-facing task has a paired `TRD-NNN-TEST` verification task.

### Cluster 1 — Foundation (15h impl + 10h test)

**TRD-001: Error types + structured response schema** `[2h]` `[satisfies REQ-025]`
- **Validates PRD ACs**: AC-025-1, AC-025-2
- Implementation checklist:
  - Given any tool raises Exception, when the FastMCP wrapper catches it, then a `{success: false, error, code}` is returned (never a raw trace).
  - Given a new error code is added, when tools import `ErrorCode` enum, then only the 16 canonical codes are usable.
- `[verifies TRD-001]` `TRD-001-TEST` `[1h]`

**TRD-002: Token redaction utility** `[2h]` `[satisfies REQ-023]`
- **Validates PRD ACs**: AC-023-1, AC-023-4
- Two layers: literal replace (primary) + regex `(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{20,255}` (safety net); wired as a `logging.Filter` on the root logger.
- `[verifies TRD-002]` `TRD-002-TEST` `[2h]` — assert classic PAT AND fine-grained PAT both redacted in log output + in exception messages

**TRD-003: Config loader** `[3h]` `[satisfies REQ-023]`
- **Validates PRD ACs**: AC-002-1, AC-023-3
- Load from `~/.config/github-issue-agent/config.json`; ensure chmod 600 on write; schema-validate via Pydantic.
- `[verifies TRD-003]` `TRD-003-TEST` `[1h]` — assert `-rw-------` mode post-write; reject malformed config

**TRD-004: Atomic file writer** `[2h]` `[satisfies REQ-015]`
- **Validates PRD ACs**: AC-015-1, AC-015-2
- Write to `{target}.tmp.{pid}.{ts}` then `os.replace`; fsync before rename on POSIX.
- `[verifies TRD-004]` `TRD-004-TEST` `[2h]` — simulate crash between write and rename; original unchanged

**TRD-005: Path-traversal guard utility** `[2h]` `[satisfies REQ-014]`
- **Validates PRD ACs**: AC-014-1, AC-014-2, AC-014-3
- `resolve_inside(repo_root, path) -> Path | raises PathTraversalError` — uses `Path.resolve(strict=False)` then `is_relative_to(repo_root)`; resolves symlinks and rejects if target escapes repo.
- `[verifies TRD-005]` `TRD-005-TEST` `[2h]` — cases: `..`, absolute path, symlink-escape, valid relative

**TRD-006: SessionStore singleton** `[4h]` `[satisfies REQ-006]`
- **Validates PRD ACs**: AC-006-1, AC-006-2, AC-006-3
- `asyncio.Lock`-guarded writer; read returns dict snapshot; corruption → `session.json.bak-{ts}` rotation.
- `[verifies TRD-006]` `TRD-006-TEST` `[2h]` — persistence across process restart; bad-JSON recovery

### Cluster 2 — Setup (13h impl + 5h test)

**TRD-007: install.sh bootstrapper** `[3h]` `[satisfies REQ-001, REQ-004]`
- **Validates PRD ACs**: AC-001-1, AC-001-2, AC-001-3, AC-004-1
- Python 3.10 check → `pip install` → run `setup.py` → `claude mcp add`.
- `[verifies TRD-007]` `TRD-007-TEST` `[1h]` — shellcheck + mocked-env idempotency

**TRD-008: Token validation function** `[2h]` `[satisfies REQ-003]`
- **Validates PRD ACs**: AC-003-1, AC-003-2, AC-003-3
- `validate_token(token) -> {user, scopes}`; raises on 401/403.
- `[verifies TRD-008]` `TRD-008-TEST` `[1h]` — mock GET /user 200/401/403, check scope warnings

**TRD-009: Test-runner / linter auto-detection** `[3h]` `[satisfies REQ-002]`
- **Validates PRD ACs**: AC-002-4, AC-002-5
- File-signal matrix per spec (pyproject.toml, package.json, Cargo.toml, go.mod, Gemfile+spec/, pom.xml; ruff.toml, .eslintrc, rubocop.yml).
- `[verifies TRD-009]` `TRD-009-TEST` `[1h]` — fixture repos per ecosystem; confirm detection

**TRD-010: Setup wizard CLI** `[5h]` `[satisfies REQ-002, REQ-003]`
- **Validates PRD ACs**: AC-002-1 through AC-002-5
- `rich.prompt`-based bulk flow; re-entrant via `/issue-agent setup`.
- `[verifies TRD-010]` `TRD-010-TEST` `[2h]` — scripted stdin happy path + re-run preserving defaults

### Cluster 3 — Core Control (13h impl + 5h test)

**TRD-011: MCP server bootstrap, idle-by-default** `[3h]` `[satisfies REQ-005, REQ-009, INFRA]`
- **Validates PRD ACs**: AC-005-1, AC-005-2, AC-009-1, AC-009-2
- FastMCP initialization; tool registration; `/issue-agent help` slash-command table.
- `[verifies TRD-011]` `TRD-011-TEST` `[1h]` — MCP loads idle; no polling task created

**TRD-012: Control tools** `[5h]` `[satisfies REQ-005, REQ-007, REQ-008, REQ-009]` `[depends: TRD-006, TRD-013]`
- **Validates PRD ACs**: AC-007-1 through AC-007-5, AC-008-1, AC-008-2
- `issue_agent_{start,stop,status,set_mode,fetch_now}`. Immediate mode switch verified by per-checkpoint `should_prompt_user()` helper reading live SessionStore.
- `[verifies TRD-012]` `TRD-012-TEST` `[2h]` — full→semi and semi→full mid-flow; stop cancels polling task

**TRD-013: Protocol template renderer** `[2h]` `[satisfies REQ-020]` `[depends: TRD-033]`
- **Validates PRD ACs**: AC-020-1, AC-020-2
- Jinja2-style (or f-string) substitution; omits semi section when mode=full.
- `[verifies TRD-013]` `TRD-013-TEST` `[1h]` — mode-dependent section omission

**TRD-014: Convention discovery (Step 0)** `[3h]` `[satisfies REQ-020b]` `[depends: TRD-006, TRD-022]`
- **Validates PRD ACs**: AC-020b-1, AC-020b-2, AC-020b-3
- Reads CLAUDE.md, CONTRIBUTING.md, AGENTS.md, `.cursor/rules/*.md`, `.editorconfig`, README.md; summary cached in SessionStore.
- `[verifies TRD-014]` `TRD-014-TEST` `[1h]` — present/absent cases; cache reuse across issues

### Cluster 4 — Issue Ops & Picker (25h impl + 14h test)

**TRD-015: GitHub client wrapper** `[3h]` `[satisfies REQ-023, REQ-024, REQ-025]` `[depends: TRD-002, TRD-001]`
- **Validates PRD ACs**: AC-024-1, AC-024-2
- PyGithub + httpx; token passed once at construction; logger wired through redaction filter; rate-limit parser reads `X-RateLimit-Remaining` + `X-RateLimit-Reset`.
- `[verifies TRD-015]` `TRD-015-TEST` `[1h]` — mocked 403 rate-limit parsed; token never in exception text

**TRD-016: Issue tools** `[4h]` `[satisfies REQ-010, REQ-013]` `[depends: TRD-015]`
- **Validates PRD ACs**: AC-010-1, AC-010-2, AC-013-1, AC-013-2
- `list_issues`, `get_issue`, `pick_issues`, `skip_issue`, `post_issue_comment`.
- `[verifies TRD-016]` `TRD-016-TEST` `[2h]` — priority derivation, label filter, comment post verified

**TRD-017: Duplicate PR detection** `[3h]` `[satisfies REQ-013b]` `[depends: TRD-015, TRD-023]`
- **Validates PRD ACs**: AC-013b-1, AC-013b-2, AC-013b-3
- `check_issue_has_open_pr(n)` — search open PRs via `gh pr list --json` + local branch probe; warn user, never auto-skip.
- `[verifies TRD-017]` `TRD-017-TEST` `[2h]` — dup via body keyword; dup via branch; no dup

**TRD-018: UI server (Starlette sub-app)** `[4h]` `[satisfies REQ-011]` `[depends: TRD-016, TRD-006]`
- **Validates PRD ACs**: AC-011-1, AC-011-6
- Routes: `GET /api/issues`, `POST /api/confirm`, `GET /` serves picker.html; bind 127.0.0.1 only.
- `[verifies TRD-018]` `TRD-018-TEST` `[2h]` — API contract tests

**TRD-019a: picker.html — layout, cards, filters, search** `[3h]` `[satisfies REQ-011]` `[depends: TRD-018]`
- **Validates PRD ACs**: AC-011-3, AC-011-5
- Self-contained HTML; no external deps; light/dark auto.
- `[verifies TRD-019a]` `TRD-019a-TEST` `[2h]` — Playwright: search filters, mobile reflow

**TRD-019b: picker.html — keyboard & submit flow** `[3h]` `[satisfies REQ-011]` `[depends: TRD-019a]`
- **Validates PRD ACs**: AC-011-4, AC-011-6
- Space/Enter/Escape; POST /api/confirm; auto-close tab.
- `[verifies TRD-019b]` `TRD-019b-TEST` `[2h]` — keyboard events + confirm POST

**TRD-020: Terminal fallback picker** `[3h]` `[satisfies REQ-011]` `[depends: TRD-016]`
- **Validates PRD ACs**: AC-011-2
- `rich.prompt.Confirm` + interactive checkbox list; same JSON contract as browser UI.
- `[verifies TRD-020]` `TRD-020-TEST` `[2h]` — scripted stdin

**TRD-021: UI opener with headless detection** `[2h]` `[satisfies REQ-011]` `[depends: TRD-018, TRD-020]`
- **Validates PRD ACs**: AC-011-1, AC-011-2
- `webbrowser.open()` with probe: `$DISPLAY` empty + `$SSH_CONNECTION` set + `xdg-open`/`open` missing → terminal path.
- `[verifies TRD-021]` `TRD-021-TEST` `[1h]` — mocked env var matrix

### Cluster 5 — Code & Git Ops (27h impl + 13h test)

**TRD-022: Filesystem tools** `[4h]` `[satisfies REQ-014, REQ-015]` `[depends: TRD-004, TRD-005]`
- **Validates PRD ACs**: AC-014-1, AC-014-2, AC-014-3, AC-015-1, AC-015-2
- `read_file`, `write_file`, `list_directory`, `search_codebase`, `get_repo_structure`, `read_multiple_files`.
- `[verifies TRD-022]` `TRD-022-TEST` `[2h]`

**TRD-023: Git tools + default-branch detection** `[5h]` `[satisfies REQ-016]`
- **Validates PRD ACs**: AC-016-1 through AC-016-6
- `create_branch`, `git_diff`, `commit_changes`, `push_branch`, `get_current_branch`, `get_default_branch`; all via `asyncio.to_thread(subprocess.run)`; branch-name uniqueness retry (-v2/-v3/...).
- `[verifies TRD-023]` `TRD-023-TEST` `[2h]` — fixture repos w/ main, master, develop defaults

**TRD-024: PR creation** `[4h]` `[satisfies REQ-018, REQ-021]` `[depends: TRD-023, TRD-015]`
- **Validates PRD ACs**: AC-018-1, AC-018-2, AC-018-3, AC-021-2
- `create_pr(title, body, draft)`; prefers `gh pr create`, falls back to PyGithub; enforces "Closes #N" in body; default `draft=true` when SessionStore.mode == "full".
- `[verifies TRD-024]` `TRD-024-TEST` `[2h]` — draft in full mode; closes marker present

**TRD-025: Linting tool** `[3h]` `[satisfies REQ-017b]` `[depends: TRD-023]`
- **Validates PRD ACs**: AC-017b-1, AC-017b-2, AC-017b-3
- `check_linting()` runs configured linter on `git diff --name-only` output; allow-list validated.
- `[verifies TRD-025]` `TRD-025-TEST` `[1h]` — ruff fixture; bad-command rejection

**TRD-026a: Docker sandbox — happy path + unavailable** `[3h]` `[satisfies REQ-017]`
- **Validates PRD ACs**: AC-017-1, AC-017-2
- Docker SDK client; mount repo RO + `/tmp/test-output` RW; 10-min wall clock; structured `{passed, failed, errors, output}`.
- `[verifies TRD-026a]` `TRD-026a-TEST` `[2h]` — live container with fixture; mocked DOCKER_UNAVAILABLE

**TRD-026b: Docker sandbox — allow-list + read-only enforcement** `[3h]` `[satisfies REQ-017]` `[depends: TRD-026a]`
- **Validates PRD ACs**: AC-017-3, AC-017-4
- Allow-list regex at config load; runtime escape attempt (write outside output volume) returns failure.
- `[verifies TRD-026b]` `TRD-026b-TEST` `[1h]`

**TRD-027: Retry wrapper for full-auto** `[2h]` `[satisfies REQ-022]` `[depends: TRD-025, TRD-026a]`
- **Validates PRD ACs**: AC-022-1, AC-022-2
- `@with_retries(max=3)` around lint+test block; on final failure labels issue `human-review`.
- `[verifies TRD-027]` `TRD-027-TEST` `[1h]`

**TRD-028: Undo / rollback** `[3h]` `[satisfies REQ-019]` `[depends: TRD-023]`
- **Validates PRD ACs**: AC-019-1, AC-019-2, AC-019-3
- `undo_last_change()`; checks commit author matches configured identity; refuses on default branch (dynamic).
- `[verifies TRD-028]` `TRD-028-TEST` `[2h]`

### Cluster 6 — Polish (9h impl + 5h test)

**TRD-029: Serial queue processor** `[3h]` `[satisfies REQ-012, REQ-024]` `[depends: TRD-006, TRD-016]`
- **Validates PRD ACs**: AC-012-1, AC-012-2, AC-012-3
- Orchestrator: pick next issue, set active, process to terminal state, advance; >10 items → warning.
- Includes **pause-on-NETWORK_ERROR** behavior (preserve state; resume on next successful tool call).
- `[verifies TRD-029]` `TRD-029-TEST` `[2h]`

**TRD-030: Polling task** `[3h]` `[satisfies REQ-008]` `[depends: TRD-016, TRD-006]`
- **Validates PRD ACs**: AC-008-1, AC-008-2, AC-008-3
- `asyncio.create_task` loop sleeping `poll_interval_min * 60`s; swallows transient errors.
- `[verifies TRD-030]` `TRD-030-TEST` `[1h]`

**TRD-031: Network & rate-limit handling** `[2h]` `[satisfies REQ-024]` `[depends: TRD-015]`
- **Validates PRD ACs**: AC-024-1, AC-024-2
- Parse `X-RateLimit-Reset` → human-readable reset time.
- `[verifies TRD-031]` `TRD-031-TEST` `[1h]`

**TRD-032: Naming helpers** `[1h]` `[satisfies REQ-021]`
- **Validates PRD ACs**: AC-021-1, AC-021-2
- `slugify(title, max=40)`, `branch_name(n, title)`, `commit_msg(desc, n)`, `pr_title(title, n)`.
- `[verifies TRD-032]` `TRD-032-TEST` `[1h]`

### Cluster 7 — Docs & Packaging (7h impl + 1.5h test)

**TRD-033: agent_protocol.md content** `[3h]` `[satisfies REQ-020, REQ-020b]`
- **Validates PRD ACs**: AC-020-1, AC-020-2, AC-020b-1, AC-020b-2
- Full semi+full workflow text; Step 0 conventions; placeholder vars.
- `[verifies TRD-033]` `TRD-033-TEST` `[1h]` — placeholder coverage audit

**TRD-034: README.md** `[3h]` `[satisfies REQ-001]`
- All 8 sections per PRD (Install, Quick Start, Commands, Modes, Token howto, Security, Troubleshooting).
- `[verifies TRD-034]` `TRD-034-TEST` `[0.5h]` — section-presence audit via markdown parser

**TRD-035: Packaging** `[1h]`
- `pyproject.toml` with entry-point, `.gitignore` (already drafted), `requirements.txt` (pinned).
- `[satisfies INFRA]`

### Infra

**TRD-INFRA-01: asyncio app composition root** `[2h]` `[satisfies ARCH]`
- `ghia/app.py` wires FastMCP + Starlette + SessionStore + polling; exposes `create_app()`.
- `[verifies TRD-INFRA-01]` `TRD-INFRA-01-TEST` `[1h]`

**TRD-INFRA-02: Playwright test harness** `[1h]` `[satisfies ARCH]`
- Playwright install + fixture launcher for `picker.html` tests.

---

## 4. Acceptance Criteria Traceability Matrix

| REQ-NNN | Description | Implementation Tasks | Test Tasks |
|---|---|---|---|
| REQ-001 | One-command install | TRD-007, TRD-034 | TRD-007-TEST, TRD-034-TEST |
| REQ-002 | Bulk setup wizard + detection | TRD-009, TRD-010 | TRD-009-TEST, TRD-010-TEST |
| REQ-003 | Token validation | TRD-008, TRD-010 | TRD-008-TEST |
| REQ-004 | MCP registration | TRD-007 | TRD-007-TEST |
| REQ-005 | Idle-by-default | TRD-011, TRD-012 | TRD-011-TEST, TRD-012-TEST |
| REQ-006 | Session state persistence | TRD-006 | TRD-006-TEST |
| REQ-007 | Semi/full modes, immediate switch | TRD-012, TRD-013 | TRD-012-TEST, TRD-013-TEST |
| REQ-008 | Polling timer | TRD-012, TRD-030 | TRD-030-TEST |
| REQ-009 | Slash commands | TRD-011, TRD-012 | TRD-011-TEST, TRD-012-TEST |
| REQ-010 | List/fetch issues | TRD-016 | TRD-016-TEST |
| REQ-011 | Picker UI + terminal fallback | TRD-018, TRD-019a, TRD-019b, TRD-020, TRD-021 | TRD-018-TEST … TRD-021-TEST |
| REQ-012 | Serial queue | TRD-029 | TRD-029-TEST |
| REQ-013 | Progress comments | TRD-016 | TRD-016-TEST |
| REQ-013b | Duplicate-PR detection | TRD-017 | TRD-017-TEST |
| REQ-014 | Path-traversal protection | TRD-005, TRD-022 | TRD-005-TEST, TRD-022-TEST |
| REQ-015 | Atomic writes | TRD-004, TRD-022 | TRD-004-TEST, TRD-022-TEST |
| REQ-016 | Git ops + default-branch | TRD-023 | TRD-023-TEST |
| REQ-017 | Docker sandbox tests | TRD-026a, TRD-026b | TRD-026a-TEST, TRD-026b-TEST |
| REQ-017b | Linting | TRD-025 | TRD-025-TEST |
| REQ-018 | Draft PRs / no auto-merge | TRD-024 | TRD-024-TEST |
| REQ-019 | Undo / rollback | TRD-028 | TRD-028-TEST |
| REQ-020 | Auto-inject protocol | TRD-013, TRD-033 | TRD-013-TEST, TRD-033-TEST |
| REQ-020b | Convention awareness | TRD-014, TRD-033 | TRD-014-TEST |
| REQ-021 | Naming conventions | TRD-024, TRD-032 | TRD-024-TEST, TRD-032-TEST |
| REQ-022 | 3-retry limit | TRD-027 | TRD-027-TEST |
| REQ-023 | Token security | TRD-002, TRD-003, TRD-015 | TRD-002-TEST, TRD-003-TEST, TRD-015-TEST |
| REQ-024 | Network degradation | TRD-029, TRD-031 | TRD-029-TEST, TRD-031-TEST |
| REQ-025 | Structured errors | TRD-001, TRD-015 | TRD-001-TEST, TRD-015-TEST |

**Coverage summary**: 28 PRD requirements → 100% have at least one implementation task and one test task. 0 orphaned `[satisfies]` annotations.

---

## 5. Sprint Planning

> Estimates are solo hours. Tests are paired with impl tasks inside each sprint — do not defer tests to the end.

### Sprint 1 — Foundation & Setup (~33h)
**Goal**: safe primitives + installable artifact.
- TRD-001, TRD-002, TRD-003, TRD-004, TRD-005, TRD-006 (+ tests)
- TRD-007, TRD-008, TRD-009, TRD-010 (+ tests)
- **Exit criteria**: `install.sh` runs to completion; `config.json` written with chmod 600; SessionStore survives restart.

### Sprint 2 — Core Control & Issue Ops (~41h)
**Goal**: MCP registered; user can fetch and list issues.
- TRD-INFRA-01 (+ test)
- TRD-011, TRD-012, TRD-013, TRD-014 (+ tests)
- TRD-015, TRD-016 (+ tests)
- TRD-033 (agent_protocol.md content) (+ test)
- **Exit criteria**: `/issue-agent start` returns populated protocol; `list_issues` works end-to-end.

### Sprint 3 — Picker UI & Duplicate Detection (~22h)
**Goal**: user can pick issues via browser or terminal.
- TRD-018, TRD-019a, TRD-019b, TRD-020, TRD-021 (+ tests)
- TRD-INFRA-02 (Playwright harness)
- TRD-017 (+ test)
- **Exit criteria**: full pick → confirm → queue-persisted flow; headless fallback works.

### Sprint 4 — Code & Git Ops (~40h)
**Goal**: Claude can actually write a fix end-to-end.
- TRD-022, TRD-023, TRD-024 (+ tests)
- TRD-025, TRD-026a, TRD-026b (+ tests)
- TRD-027, TRD-028 (+ tests)
- TRD-032 (+ test)
- **Exit criteria**: E2E on one real issue in a sandbox repo → PR opened as draft with "Closes #N".

### Sprint 5 — Polish & Ship (~18h)
**Goal**: production-ready MVP.
- TRD-029, TRD-030, TRD-031 (+ tests)
- TRD-034, TRD-035
- Manual E2E on real GitHub repo; dogfood for 1 week.
- **Exit criteria**: README complete; 10-issue dogfood run completes ≥80% success.

**Total**: ~154 hours impl + paired tests. Buffer ~10% for integration surprises → **~170 hours**.

---

## 5b. Team Configuration

> **Auto-generated by `/ensemble:configure-team` on 2026-04-23.** Review agent assignments below and edit if needed before running `/ensemble:implement-trd-beads`.

### 5b.1 Complexity Metrics
| Metric | Value |
|---|---|
| Task count | 37 impl + 35 test = 72 total |
| Estimated hours | ~170 h |
| Domain count | 6 |
| Domains | backend/Python async, frontend/HTML+JS UI, git/VCS, docker/sandbox, devops/install, docs/prompts |
| Cross-cutting tasks | 8 |
| Longest dependency chain | 5 (TRD-006 → 015 → 017 → 018 → 019a) |
| **Tier** | **COMPLEX** |

### 5b.2 Tier Rationale
All three Complex-tier thresholds exceeded:
- `task_count (37) > 25` ✓
- `domain_count (6) >= 3` ✓
- `estimated_hours (170) > 60` ✓

### 5b.3 Marketplace Gap Analysis
- `marketplace.json` not present in project root → **gap analysis skipped**.
- Using globally-installed `ensemble-full` agent catalog discovered at session init.
- No plugins installed during this configuration run.

### 5b.4 Agent Assignments

| Role | Agent | Owns |
|---|---|---|
| Lead | `ensemble-full:tech-lead-orchestrator` | task-selection, architecture-review, final-approval, cross-domain coordination |
| Builder — Python async / MCP tools | `ensemble-full:backend-developer` | TRD-001..006, 008..017, 022, 025, 027, 029..032, INFRA-01 |
| Builder — Picker UI (HTML/JS) | `ensemble-full:frontend-developer` | TRD-018, 019a, 019b, 020, 021 |
| Builder — Git / PR operations | `ensemble-full:git-workflow` + `ensemble-full:github-specialist` | TRD-023, 024, 028 (git-workflow for commits/naming; github-specialist for gh-CLI + PR API) |
| Builder — Docker sandbox + install | `ensemble-full:infrastructure-developer` | TRD-007, 026a, 026b, 035, INFRA-02 |
| Builder — Protocol + docs | `ensemble-full:documentation-specialist` | TRD-033, 034 |
| Reviewer | `ensemble-full:code-reviewer` | security-sensitive tasks (TRD-002, 005, 014, 022, 026b, 028), async-hygiene review on all backend tasks |
| QA | `ensemble-full:qa-orchestrator` + `ensemble-full:test-runner` | all TRD-*-TEST tasks, sprint-exit E2E |

### 5b.5 Team Configuration (YAML)

```yaml
team:
  tier: complex
  roles:
    lead:
      agent: ensemble-full:tech-lead-orchestrator
      owns:
        - task-selection
        - architecture-review
        - final-approval
        - cross-domain-coordination
    builders:
      - agent: ensemble-full:backend-developer
        owns_domains: [python-async, mcp-tools, session-state, config, security-primitives, polling]
        owns_tasks:
          [TRD-001, TRD-002, TRD-003, TRD-004, TRD-005, TRD-006,
           TRD-008, TRD-009, TRD-010, TRD-011, TRD-012, TRD-013, TRD-014,
           TRD-015, TRD-016, TRD-017, TRD-022, TRD-025, TRD-027,
           TRD-029, TRD-030, TRD-031, TRD-032, TRD-INFRA-01]
      - agent: ensemble-full:frontend-developer
        owns_domains: [picker-ui, accessibility, keyboard-nav, browser-api]
        owns_tasks: [TRD-018, TRD-019a, TRD-019b, TRD-020, TRD-021]
      - agent: ensemble-full:git-workflow
        owns_domains: [commit-conventions, branch-naming, semantic-versioning]
        owns_tasks: [TRD-023, TRD-032]
      - agent: ensemble-full:github-specialist
        owns_domains: [gh-cli, pr-creation, duplicate-pr-detection]
        owns_tasks: [TRD-017, TRD-024, TRD-028]
      - agent: ensemble-full:infrastructure-developer
        owns_domains: [docker-sandbox, install-scripts, packaging, test-harness]
        owns_tasks: [TRD-007, TRD-026a, TRD-026b, TRD-035, TRD-INFRA-02]
      - agent: ensemble-full:documentation-specialist
        owns_domains: [readme, agent-protocol, user-docs]
        owns_tasks: [TRD-033, TRD-034]
    reviewer:
      agent: ensemble-full:code-reviewer
      priority_tasks:
        [TRD-002, TRD-003, TRD-005, TRD-014, TRD-022, TRD-026b, TRD-028]
      review_checklist:
        - security-boundary-enforcement
        - async-hygiene (no blocking calls in event loop)
        - error-code-correctness (one of 16 defined codes)
        - atomic-write-guarantees
    qa:
      primary: ensemble-full:qa-orchestrator
      executor: ensemble-full:test-runner
      owns: all TRD-*-TEST tasks
      sprint_exit_gates:
        - sprint-1: install.sh idempotent, config chmod 600, SessionStore round-trip
        - sprint-2: MCP registered, list_issues E2E
        - sprint-3: picker flow (browser + terminal)
        - sprint-4: one real issue → draft PR with Closes #N
        - sprint-5: 10-issue dogfood run ≥ 80% success
  handoff_contract:
    between_sprints: tech-lead-orchestrator approves exit criteria before next sprint starts
    between_builder_and_reviewer: reviewer invoked after every PR is ready but before merge
    between_builder_and_qa: qa runs paired TEST task immediately after impl task
```

### 5b.6 Cross-Cutting Coordination Notes
- **TRD-017 (duplicate PR)** has overlap between `github-specialist` (gh CLI) and `backend-developer` (session state integration) — github-specialist owns the integration module, backend-developer owns the call site inside the queue processor.
- **TRD-023 (git tools)** overlaps git-workflow and backend-developer — git-workflow sets the subprocess patterns and branch-naming conventions, backend-developer wires them into async tool modules via `asyncio.to_thread`.
- **Security-sensitive tasks** get mandatory review from `code-reviewer` before merge regardless of builder agent.

---

## 6. Quality Requirements

### 6.1 Testing Standards
- **Unit coverage**: ≥ 80% line coverage across `ghia/` modules (measured with pytest-cov).
- **Security-sensitive ACs get negative tests**: path traversal, token redaction, allow-list bypass attempts.
- **UI tests**: Playwright for `picker.html`; run in CI in headed + headless modes.
- **Test framework**: pytest + pytest-asyncio. (Note: `jest` is JS-only and not applicable to this Python codebase; if the picker UI gains a JS test surface beyond Playwright, revisit.)

### 6.2 Security
- Token redaction verified by dedicated `TRD-002-TEST` against classic + fine-grained tokens.
- `config.json` permission check (`stat -c %a`) asserted at setup and startup.
- `run_tests` allow-list regex is a hard boundary — no config value that fails the regex is ever loaded.
- Docker container runs as `--user=65534:65534` (nobody) with repo mounted read-only.

### 6.3 Performance
- Tool call overhead (excluding subprocess time) < 50ms p99.
- `list_issues` first call < 2s for repos ≤ 100 matching issues.
- Polling interval default 30 min; minimum accepted 5 min (prevents API spam).

### 6.4 Observability
- All tool errors emitted via Python logger at WARNING or ERROR with a `code` field.
- SessionStore transitions logged at INFO.
- Token never appears in any log (enforced by redaction filter).

### 6.5 Accessibility
- `picker.html` meets WCAG AA: keyboard-navigable (already in ACs), visible focus ring, color contrast ≥ 4.5:1 in both themes, screen-reader labels on checkboxes.

---

## 7. Adversarial Review Findings (applied)

### 7.1 Architecture Issues (2 found, both resolved)
1. **Race condition risk** — polling + UI + tool calls could mutate SessionStore simultaneously.
   **Resolution**: single-writer pattern via `asyncio.Lock` on SessionStore; documented in §2.5.
2. **Undefined interface — duplicate PR search** could diverge between `gh` and PyGithub backends.
   **Resolution**: normalized wrapper in `ghia/integrations/` returns the same shape regardless of backend; test covers both paths.

### 7.2 Coverage Issues (2 found, both resolved)
3. **REQ-024 (network degradation)** originally only reflected in TRD-031 (error parsing). Missing actual queue pause behavior.
   **Resolution**: pause-on-NETWORK_ERROR requirement added to TRD-029.
4. **REQ-007 AC-007-5 (semi→full mid-issue)** needed runtime-readable mode, not just initial protocol render.
   **Resolution**: `should_prompt_user()` helper reads SessionStore.mode live; TRD-012 explicitly covers both directions.

### 7.3 Dependency / Estimate Issues (1 flagged, resolved)
5. **TRD-019 (picker.html, original 6h)** was too large for a single task unit.
   **Resolution**: split into TRD-019a (layout/filter/search, 3h) + TRD-019b (keyboard/submit, 3h).

### 7.4 Testability Issues (1 found, resolved)
6. **TRD-019-TEST** was "click through filter/search/select" — too vague.
   **Resolution**: paired 019a-TEST / 019b-TEST cite specific ACs (AC-011-3, AC-011-4, AC-011-5, AC-011-6).

---

## 8. Design Readiness Scorecard

| Dimension | Score (1-5) | Rationale |
|---|---|---|
| **Architecture completeness** | 4.0 | All components, data flows, and integration points defined. Concurrency model explicit. One open question noted: behavior when Docker daemon restarts mid-test (treated as `TEST_FAILED` — explicit retry policy deferred to implementation). |
| **Task coverage** | 4.5 | Every PRD REQ-NNN has ≥1 impl + ≥1 test task; orphaned-annotation count = 0; traceability matrix complete. |
| **Dependency clarity** | 4.0 | Explicit `[depends: ...]` annotations; no cycles. Longest chain: TRD-006 → TRD-015 → TRD-017 → TRD-018 → TRD-019a (depth 5) — acceptable given each layer is narrow. |
| **Estimate confidence** | 4.0 | Tasks granular (most 1-5h); two 6h outliers split into 3h+3h pairs. Solo estimates carry inherent uncertainty — 10% buffer added at sprint level. |
| **Overall** | **4.13** | **PASS** (threshold 4.0) |

**Gate Decision**: ✅ **PASS** — TRD is ready for implementation handoff.

---

## 9. Open Questions & Deferred Decisions

| # | Question | Deferred to |
|---|---|---|
| OQ-1 | Docker daemon restart mid-test: retry once, or fail immediately? | Implementation of TRD-026a |
| OQ-2 | `gh` CLI version floor (features like `--json` require ≥2.0) — minimum version check? | TRD-024 |
| OQ-3 | Playwright test execution in CI — headless? matrix of browsers? | TRD-INFRA-02 |
| OQ-4 | `rich` terminal picker rendering on Windows Terminal vs. PowerShell vs. WSL — which is primary? | TRD-020 |

---

## 10. Next Steps

1. **Optional**: `/ensemble:configure-team docs/TRD/TRD-2026-001-github-issue-agent.md` — auto-configure specialist agents.
2. **Implement**: `/ensemble:implement-trd-beads docs/TRD/TRD-2026-001-github-issue-agent.md` — execute the task breakdown with bead-based project management.
3. Alternatively, follow Sprint 1 manually starting with TRD-001.
