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

> Task format: `- [ ] **TRD-NNN**: {description} (Nh) [satisfies REQ-NNN(-X)...]`
> Every user-facing task has a paired `TRD-NNN-TEST` verification task.

### Cluster 1 — Foundation (15h impl + 10h test)

- [ ] **TRD-001**: Error types + structured response schema (2h) [satisfies REQ-025]
  - **Validates PRD ACs**: AC-025-1, AC-025-2
  - **Implementation AC**:
    - Given any tool raises Exception, when the FastMCP wrapper catches it, then a `{success: false, error, code}` is returned (never a raw trace).
    - Given a new error code is added, when tools import `ErrorCode` enum, then only the 16 canonical codes are usable.

- [ ] **TRD-001-TEST**: Verify error types + structured response schema (1h) [verifies TRD-001] [satisfies REQ-025]
  - **Test AC**:
    - Given TRD-001 is implemented, when unit tests run, then AC-025-1 and AC-025-2 both pass.
    - Given all 16 canonical error codes exist in the `ErrorCode` enum, when the test suite runs, then enum membership is verified and no ad-hoc error strings remain.

- [ ] **TRD-002**: Token redaction utility (2h) [satisfies REQ-023]
  - **Validates PRD ACs**: AC-023-1, AC-023-4
  - **Implementation AC**:
    - Given the logger emits a record containing a token, when the redaction filter runs, then the token is literal-replaced before handler emission.
    - Given a token does not appear in the literal table, when the regex `(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{20,255}` matches, then the match is redacted (safety net).

- [ ] **TRD-002-TEST**: Verify token redaction utility (2h) [verifies TRD-002] [satisfies REQ-023]
  - **Test AC**:
    - Given a classic PAT `ghp_...` is logged, when the log output is captured, then the token never appears verbatim.
    - Given a fine-grained PAT `github_pat_...` appears in an exception message, when the exception is logged, then the token is redacted in both the log record and the exception text.

- [ ] **TRD-003**: Config loader (3h) [satisfies REQ-023]
  - **Validates PRD ACs**: AC-002-1, AC-023-3
  - **Implementation AC**:
    - Given `~/.config/github-issue-agent/config.json` exists, when the loader runs, then the file is Pydantic-validated and loaded into memory.
    - Given the loader writes a new config, when the file is persisted, then its mode is chmod 600.

- [ ] **TRD-003-TEST**: Verify config loader (1h) [verifies TRD-003] [satisfies REQ-023]
  - **Test AC**:
    - Given a freshly written `config.json`, when `stat -c %a` is invoked, then the mode is `600` (`-rw-------`).
    - Given a malformed config file, when the loader parses it, then a validation error is raised and the load is rejected.

- [ ] **TRD-004**: Atomic file writer (2h) [satisfies REQ-015]
  - **Validates PRD ACs**: AC-015-1, AC-015-2
  - **Implementation AC**:
    - Given a target file path, when the writer persists new content, then it writes to `{target}.tmp.{pid}.{ts}` and then `os.replace` into place.
    - Given a POSIX filesystem, when the rename is performed, then `fsync` has been called on the temp file before the rename.

- [ ] **TRD-004-TEST**: Verify atomic file writer (2h) [verifies TRD-004] [satisfies REQ-015]
  - **Test AC**:
    - Given a crash is simulated between write and rename, when the test inspects the target path, then the original file content is unchanged.
    - Given an atomic write completes successfully, when the file is re-read, then the new content is present without any leftover `.tmp` artifacts.

- [ ] **TRD-005**: Path-traversal guard utility (2h) [satisfies REQ-014]
  - **Validates PRD ACs**: AC-014-1, AC-014-2, AC-014-3
  - **Implementation AC**:
    - Given a path attempts to escape via `..` or an absolute path, when `resolve_inside(repo_root, path)` is called, then `PathTraversalError` is raised.
    - Given a symlink points outside `repo_root`, when `resolve_inside` resolves the target, then it rejects the path after `Path.resolve(strict=False)` + `is_relative_to(repo_root)` check.

- [ ] **TRD-005-TEST**: Verify path-traversal guard utility (2h) [verifies TRD-005] [satisfies REQ-014]
  - **Test AC**:
    - Given cases `..`, absolute path, symlink-escape, and a valid relative path, when each is passed to `resolve_inside`, then the first three raise `PathTraversalError` and the last returns a resolved `Path`.
    - Given AC-014-1, AC-014-2, AC-014-3, when the test suite runs, then every listed AC passes.

- [ ] **TRD-006**: SessionStore singleton (4h) [satisfies REQ-006]
  - **Validates PRD ACs**: AC-006-1, AC-006-2, AC-006-3
  - **Implementation AC**:
    - Given multiple async tasks try to mutate state, when they go through the SessionStore, then all writes serialize through `asyncio.Lock` and read returns an immutable dict snapshot.
    - Given `session.json` is corrupted, when the store loads, then it rotates the file to `session.json.bak-{ts}` and starts fresh.

- [ ] **TRD-006-TEST**: Verify SessionStore singleton (2h) [verifies TRD-006] [satisfies REQ-006]
  - **Test AC**:
    - Given the process is restarted, when the SessionStore reloads, then state written before the restart is still present.
    - Given a bad-JSON `session.json`, when the store initializes, then the corrupt file is rotated to `.bak-{ts}` and a fresh empty session is created.

### Cluster 2 — Setup (13h impl + 5h test)

- [ ] **TRD-007**: install.sh bootstrapper (3h) [satisfies REQ-001, REQ-004]
  - **Validates PRD ACs**: AC-001-1, AC-001-2, AC-001-3, AC-004-1
  - **Implementation AC**:
    - Given a machine with Python ≥3.10, when `install.sh` runs, then it pip-installs the package, invokes `setup.py` wizard, and registers via `claude mcp add`.
    - Given Python is missing or <3.10, when `install.sh` runs, then it exits with a clear diagnostic and non-zero status.

- [ ] **TRD-007-TEST**: Verify install.sh bootstrapper (1h) [verifies TRD-007] [satisfies REQ-001, REQ-004]
  - **Test AC**:
    - Given `install.sh` is linted with shellcheck, when CI runs, then there are no errors.
    - Given a mocked environment, when `install.sh` is invoked twice, then the second run is idempotent (no duplicate MCP registration, no reinstall).

- [ ] **TRD-008**: Token validation function (2h) [satisfies REQ-003]
  - **Validates PRD ACs**: AC-003-1, AC-003-2, AC-003-3
  - **Implementation AC**:
    - Given a token, when `validate_token(token)` calls `GET /user`, then on 200 it returns `{user, scopes}`.
    - Given a token, when `validate_token` receives 401 or 403, then it raises with a structured error code.

- [ ] **TRD-008-TEST**: Verify token validation function (1h) [verifies TRD-008] [satisfies REQ-003]
  - **Test AC**:
    - Given mocked `GET /user` responses of 200/401/403, when `validate_token` is called, then success/invalid/forbidden branches are all exercised.
    - Given a token with insufficient scopes, when `validate_token` inspects the response headers, then it returns a scope warning.

- [ ] **TRD-009**: Test-runner / linter auto-detection (3h) [satisfies REQ-002]
  - **Validates PRD ACs**: AC-002-4, AC-002-5
  - **Implementation AC**:
    - Given a repo with `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, `Gemfile`+`spec/`, or `pom.xml`, when detection runs, then the correct test-runner command is proposed.
    - Given linter signals `ruff.toml`, `.eslintrc`, or `rubocop.yml`, when detection runs, then the matching linter command is proposed.

- [ ] **TRD-009-TEST**: Verify test-runner / linter auto-detection (1h) [verifies TRD-009] [satisfies REQ-002]
  - **Test AC**:
    - Given fixture repos per ecosystem (py/js/rust/go/ruby/java), when detection runs against each, then the expected test-runner command is produced.
    - Given fixture repos with various linter configs, when detection runs, then the expected linter command is produced.

- [ ] **TRD-010**: Setup wizard CLI (5h) [satisfies REQ-002, REQ-003]
  - **Validates PRD ACs**: AC-002-1 through AC-002-5
  - **Implementation AC**:
    - Given `/issue-agent setup` is invoked, when the wizard runs, then it uses `rich.prompt` for a bulk flow and persists the resulting config.
    - Given the wizard is re-entered after an initial run, when the user proceeds, then previous answers are shown as defaults and can be preserved.

- [ ] **TRD-010-TEST**: Verify setup wizard CLI (2h) [verifies TRD-010] [satisfies REQ-002, REQ-003]
  - **Test AC**:
    - Given scripted stdin for a happy-path flow, when the wizard completes, then a well-formed `config.json` is written.
    - Given a second invocation with scripted stdin accepting defaults, when the wizard completes, then previous values are preserved.

### Cluster 3 — Core Control (13h impl + 5h test)

- [ ] **TRD-011**: MCP server bootstrap, idle-by-default (3h) [satisfies REQ-005, REQ-009, INFRA]
  - **Validates PRD ACs**: AC-005-1, AC-005-2, AC-009-1, AC-009-2
  - **Implementation AC**:
    - Given the MCP process starts, when FastMCP initializes and registers tools, then no polling task is created and status is `idle`.
    - Given `/issue-agent help` is invoked, when the command runs, then a complete slash-command table is returned.

- [ ] **TRD-011-TEST**: Verify MCP server bootstrap, idle-by-default (1h) [verifies TRD-011] [satisfies REQ-005, REQ-009, INFRA]
  - **Test AC**:
    - Given the MCP process starts up, when the event loop is inspected, then no polling task exists and SessionStore status is `idle`.
    - Given `/issue-agent help` is invoked on a fresh install, when the response is parsed, then every registered slash command is listed.

- [ ] **TRD-012**: Control tools (5h) [satisfies REQ-005, REQ-007, REQ-008, REQ-009] [depends: TRD-006, TRD-013]
  - **Validates PRD ACs**: AC-007-1 through AC-007-5, AC-008-1, AC-008-2
  - **Implementation AC**:
    - Given `issue_agent_{start,stop,status,set_mode,fetch_now}` are registered, when each is called, then it manipulates SessionStore under the lock and returns a structured response.
    - Given a mode switch at mid-issue, when the next checkpoint calls `should_prompt_user()`, then the helper reads the live SessionStore mode (verifying immediate effect in both directions).

- [ ] **TRD-012-TEST**: Verify control tools (2h) [verifies TRD-012] [satisfies REQ-005, REQ-007, REQ-008, REQ-009]
  - **Test AC**:
    - Given a semi-mode issue is mid-workflow, when `set_mode("full")` is called followed by the next checkpoint, then the checkpoint is skipped; and vice versa for full→semi.
    - Given `issue_agent_stop` is invoked while polling is active, when stop completes, then the polling asyncio task is cancelled and SessionStore status is `idle`.

- [ ] **TRD-013**: Protocol template renderer (2h) [satisfies REQ-020] [depends: TRD-033]
  - **Validates PRD ACs**: AC-020-1, AC-020-2
  - **Implementation AC**:
    - Given `agent_protocol.md` with placeholder vars, when the renderer substitutes variables, then the output contains all expected substitutions (Jinja2-style or f-string).
    - Given `mode=full`, when the renderer runs, then the semi-only section is omitted from the output.

- [ ] **TRD-013-TEST**: Verify protocol template renderer (1h) [verifies TRD-013] [satisfies REQ-020]
  - **Test AC**:
    - Given `mode=full` and `mode=semi`, when the renderer produces output for each, then only the semi variant contains the semi-specific checkpoint section.
    - Given all placeholders in the template, when rendered with a full variable set, then no unresolved `{{var}}` tokens remain in the output.

- [ ] **TRD-014**: Convention discovery (Step 0) (3h) [satisfies REQ-020b] [depends: TRD-006, TRD-022]
  - **Validates PRD ACs**: AC-020b-1, AC-020b-2, AC-020b-3
  - **Implementation AC**:
    - Given the repo root, when Step 0 runs, then it reads `CLAUDE.md`, `CONTRIBUTING.md`, `AGENTS.md`, `.cursor/rules/*.md`, `.editorconfig`, and `README.md` and builds a summary.
    - Given the summary is produced, when it is written to SessionStore, then subsequent issues reuse the cached summary without re-reading files.

- [ ] **TRD-014-TEST**: Verify convention discovery (Step 0) (1h) [verifies TRD-014] [satisfies REQ-020b]
  - **Test AC**:
    - Given a fixture repo with all convention files present, when discovery runs, then the summary contains entries from every file.
    - Given a fixture repo with no convention files, when discovery runs, then it completes gracefully with an empty-but-valid summary and caches it in SessionStore.

### Cluster 4 — Issue Ops & Picker (25h impl + 14h test)

- [ ] **TRD-015**: GitHub client wrapper (3h) [satisfies REQ-023, REQ-024, REQ-025] [depends: TRD-002, TRD-001]
  - **Validates PRD ACs**: AC-024-1, AC-024-2
  - **Implementation AC**:
    - Given a token at construction, when the client is built, then its logger is wired through the redaction filter and the token is never logged.
    - Given a 403 rate-limit response, when the client parses it, then `X-RateLimit-Remaining` and `X-RateLimit-Reset` are extracted into a structured error.

- [ ] **TRD-015-TEST**: Verify GitHub client wrapper (1h) [verifies TRD-015] [satisfies REQ-023, REQ-024, REQ-025]
  - **Test AC**:
    - Given a mocked 403 rate-limit response, when the client raises, then the error carries parsed reset-time data and correct error code.
    - Given any error raised by the client, when its message is inspected, then the GitHub token never appears in the exception text.

- [ ] **TRD-016**: Issue tools (4h) [satisfies REQ-010, REQ-013] [depends: TRD-015]
  - **Validates PRD ACs**: AC-010-1, AC-010-2, AC-013-1, AC-013-2
  - **Implementation AC**:
    - Given a label filter, when `list_issues` is called, then issues matching the label are returned with derived priority metadata.
    - Given an issue number and a comment body, when `post_issue_comment` is called, then the comment is persisted on GitHub via the client wrapper.

- [ ] **TRD-016-TEST**: Verify issue tools (2h) [verifies TRD-016] [satisfies REQ-010, REQ-013]
  - **Test AC**:
    - Given a mocked list of issues with various labels, when `list_issues` applies a label filter, then only matching issues are returned and priority is correctly derived.
    - Given a mocked comment POST, when `post_issue_comment` is invoked, then the request body matches the expected comment payload.

- [ ] **TRD-017**: Duplicate PR detection (3h) [satisfies REQ-013b] [depends: TRD-015, TRD-023]
  - **Validates PRD ACs**: AC-013b-1, AC-013b-2, AC-013b-3
  - **Implementation AC**:
    - Given an issue number, when `check_issue_has_open_pr(n)` runs, then it queries `gh pr list --json` and probes local branches, returning a warning flag rather than auto-skipping.
    - Given a match is found, when the caller decides, then the caller (not this tool) chooses whether to skip.

- [ ] **TRD-017-TEST**: Verify duplicate PR detection (2h) [verifies TRD-017] [satisfies REQ-013b]
  - **Test AC**:
    - Given a PR body referencing the issue keyword and a local branch matching the issue pattern, when detection runs, then both signals are reported.
    - Given no matching PR or branch, when detection runs, then the result is a clean `no duplicate` response.

- [ ] **TRD-018**: UI server (Starlette sub-app) (4h) [satisfies REQ-011] [depends: TRD-016, TRD-006]
  - **Validates PRD ACs**: AC-011-1, AC-011-6
  - **Implementation AC**:
    - Given the sub-app mounts, when clients hit `GET /api/issues`, `POST /api/confirm`, or `GET /`, then each route returns the expected content/shape and `GET /` serves `picker.html`.
    - Given the listener binds, when inspected, then it is bound to `127.0.0.1` only (no external interface exposure).

- [ ] **TRD-018-TEST**: Verify UI server (Starlette sub-app) (2h) [verifies TRD-018] [satisfies REQ-011]
  - **Test AC**:
    - Given the server is running, when contract tests hit each route, then responses match the documented JSON schema.
    - Given the server starts, when the bind address is inspected, then it is `127.0.0.1` (not `0.0.0.0`).

- [ ] **TRD-019a**: picker.html — layout, cards, filters, search (3h) [satisfies REQ-011] [depends: TRD-018]
  - **Validates PRD ACs**: AC-011-3, AC-011-5
  - **Implementation AC**:
    - Given `picker.html` loads, when the DOM renders, then it is self-contained (no external deps) and auto-switches light/dark based on system preference.
    - Given the user types in the search box or toggles a filter, when the UI updates, then only matching issue cards remain visible.

- [ ] **TRD-019a-TEST**: Verify picker.html layout, cards, filters, search (2h) [verifies TRD-019a] [satisfies REQ-011]
  - **Test AC**:
    - Given a seeded list of issues, when Playwright types a search term, then only matching cards remain in the DOM.
    - Given a mobile viewport, when Playwright resizes the page, then the layout reflows without horizontal overflow.

- [ ] **TRD-019b**: picker.html — keyboard & submit flow (3h) [satisfies REQ-011] [depends: TRD-019a]
  - **Validates PRD ACs**: AC-011-4, AC-011-6
  - **Implementation AC**:
    - Given the picker has focus, when the user presses Space/Enter/Escape, then selection-toggle, confirm, and cancel actions fire respectively.
    - Given the user confirms, when the submit handler runs, then it POSTs to `/api/confirm` and auto-closes the tab on success.

- [ ] **TRD-019b-TEST**: Verify picker.html keyboard & submit flow (2h) [verifies TRD-019b] [satisfies REQ-011]
  - **Test AC**:
    - Given Playwright drives keyboard events, when Space/Enter/Escape are pressed, then the matching UI state changes are observed.
    - Given a confirm action, when the network is captured, then a POST to `/api/confirm` with the selected queue is recorded.

- [ ] **TRD-020**: Terminal fallback picker (3h) [satisfies REQ-011] [depends: TRD-016]
  - **Validates PRD ACs**: AC-011-2
  - **Implementation AC**:
    - Given no browser is available, when the terminal picker runs, then it presents a `rich.prompt.Confirm` plus interactive checkbox list.
    - Given the user confirms selections, when the picker returns, then the JSON contract matches the browser UI's `POST /api/confirm` shape.

- [ ] **TRD-020-TEST**: Verify terminal fallback picker (2h) [verifies TRD-020] [satisfies REQ-011]
  - **Test AC**:
    - Given scripted stdin that selects a subset of issues, when the picker exits, then the returned queue matches the expected selection.
    - Given scripted stdin that confirms without selections, when the picker exits, then an empty queue is returned without crashing.

- [ ] **TRD-021**: UI opener with headless detection (2h) [satisfies REQ-011] [depends: TRD-018, TRD-020]
  - **Validates PRD ACs**: AC-011-1, AC-011-2
  - **Implementation AC**:
    - Given a display environment is available, when the opener runs, then `webbrowser.open()` launches the picker at `localhost:4242`.
    - Given headless conditions (`$DISPLAY` empty, `$SSH_CONNECTION` set, no `xdg-open`/`open`), when the opener detects them, then it falls back to the terminal picker path.

- [ ] **TRD-021-TEST**: Verify UI opener with headless detection (1h) [verifies TRD-021] [satisfies REQ-011]
  - **Test AC**:
    - Given a mocked environment with display available, when the opener is called, then `webbrowser.open` is invoked.
    - Given a mocked headless environment matrix, when the opener is called, then the terminal fallback is selected.

### Cluster 5 — Code & Git Ops (27h impl + 13h test)

- [ ] **TRD-022**: Filesystem tools (4h) [satisfies REQ-014, REQ-015] [depends: TRD-004, TRD-005]
  - **Validates PRD ACs**: AC-014-1, AC-014-2, AC-014-3, AC-015-1, AC-015-2
  - **Implementation AC**:
    - Given any filesystem tool (`read_file`, `write_file`, `list_directory`, `search_codebase`, `get_repo_structure`, `read_multiple_files`), when it receives a path, then the path is routed through the traversal guard before any I/O.
    - Given `write_file`, when it persists content, then it uses the atomic writer (temp + `os.replace`).

- [ ] **TRD-022-TEST**: Verify filesystem tools (2h) [verifies TRD-022] [satisfies REQ-014, REQ-015]
  - **Test AC**:
    - Given an escape attempt (`..`, absolute path, symlink), when any fs tool is invoked, then the operation is rejected with a structured error.
    - Given `write_file` writes and then a crash is simulated, when the file is re-read, then either the full new content or the full old content is present (no partial writes).

- [ ] **TRD-023**: Git tools + default-branch detection (5h) [satisfies REQ-016]
  - **Validates PRD ACs**: AC-016-1 through AC-016-6
  - **Implementation AC**:
    - Given any git tool (`create_branch`, `git_diff`, `commit_changes`, `push_branch`, `get_current_branch`, `get_default_branch`), when invoked, then it runs via `asyncio.to_thread(subprocess.run)` without blocking the event loop.
    - Given a branch-name collision, when `create_branch` retries, then it appends `-v2`, `-v3`, ... until a unique name is found.

- [ ] **TRD-023-TEST**: Verify git tools + default-branch detection (2h) [verifies TRD-023] [satisfies REQ-016]
  - **Test AC**:
    - Given fixture repos whose default branch is `main`, `master`, and `develop`, when `get_default_branch` runs, then it returns the correct branch for each.
    - Given a pre-existing branch name, when `create_branch` runs, then it produces a `-v2` suffix (and `-v3` on a further collision).

- [ ] **TRD-024**: PR creation (4h) [satisfies REQ-018, REQ-021] [depends: TRD-023, TRD-015]
  - **Validates PRD ACs**: AC-018-1, AC-018-2, AC-018-3, AC-021-2
  - **Implementation AC**:
    - Given `SessionStore.mode == "full"`, when `create_pr` is called without an explicit `draft` flag, then the PR is created as a draft.
    - Given the PR body is provided, when `create_pr` builds the final body, then "Closes #N" is enforced in the text; `gh pr create` is preferred, PyGithub is the fallback.

- [ ] **TRD-024-TEST**: Verify PR creation (2h) [verifies TRD-024] [satisfies REQ-018, REQ-021]
  - **Test AC**:
    - Given `SessionStore.mode == "full"`, when `create_pr` runs against a mocked backend, then the PR payload has `draft=true`.
    - Given any PR body supplied by the caller, when the final body is assembled, then a "Closes #N" marker is always present.

- [ ] **TRD-025**: Linting tool (3h) [satisfies REQ-017b] [depends: TRD-023]
  - **Validates PRD ACs**: AC-017b-1, AC-017b-2, AC-017b-3
  - **Implementation AC**:
    - Given the configured linter and the output of `git diff --name-only`, when `check_linting()` runs, then it lints only the changed files and returns structured pass/fail results.
    - Given a linter command that fails the allow-list regex, when the config loads, then the command is rejected (never invoked).

- [ ] **TRD-025-TEST**: Verify linting tool (1h) [verifies TRD-025] [satisfies REQ-017b]
  - **Test AC**:
    - Given a ruff fixture with a known lint violation on a changed file, when `check_linting` runs, then the violation is reported in the structured result.
    - Given a linter command that does not match the allow-list, when the config tries to load it, then loading is rejected with a structured error.

- [ ] **TRD-026a**: Docker sandbox — happy path + unavailable (3h) [satisfies REQ-017]
  - **Validates PRD ACs**: AC-017-1, AC-017-2
  - **Implementation AC**:
    - Given Docker is available, when `run_tests` runs, then a container mounts the repo read-only + `/tmp/test-output` RW, a 10-minute wall clock enforces timeout, and results return as `{passed, failed, errors, output}`.
    - Given Docker is unavailable, when `run_tests` runs, then a `DOCKER_UNAVAILABLE` error is returned without a raw trace.

- [ ] **TRD-026a-TEST**: Verify Docker sandbox happy path + unavailable (2h) [verifies TRD-026a] [satisfies REQ-017]
  - **Test AC**:
    - Given a live container and a fixture test suite, when `run_tests` runs, then the structured result reflects expected pass/fail counts.
    - Given Docker is mocked as unavailable, when `run_tests` runs, then a `DOCKER_UNAVAILABLE` structured error is returned.

- [ ] **TRD-026b**: Docker sandbox — allow-list + read-only enforcement (3h) [satisfies REQ-017] [depends: TRD-026a]
  - **Validates PRD ACs**: AC-017-3, AC-017-4
  - **Implementation AC**:
    - Given a config-loaded test command, when it fails the allow-list regex at load time, then it is rejected before any container is launched.
    - Given the container attempts a write outside `/tmp/test-output` at runtime, when the escape is attempted, then the write fails because the repo mount is read-only.

- [ ] **TRD-026b-TEST**: Verify Docker sandbox allow-list + read-only enforcement (1h) [verifies TRD-026b] [satisfies REQ-017]
  - **Test AC**:
    - Given a test command that does not match the allow-list, when the config loads, then the test tool refuses to run it.
    - Given a container that tries to write to the repo mount, when the test completes, then the write attempt failed (read-only mount honored).

- [ ] **TRD-027**: Retry wrapper for full-auto (2h) [satisfies REQ-022] [depends: TRD-025, TRD-026a]
  - **Validates PRD ACs**: AC-022-1, AC-022-2
  - **Implementation AC**:
    - Given the lint+test block, when it fails, then `@with_retries(max=3)` retries up to three times.
    - Given the final retry fails, when the wrapper surrenders, then the issue is labeled `human-review` via the GitHub client.

- [ ] **TRD-027-TEST**: Verify retry wrapper for full-auto (1h) [verifies TRD-027] [satisfies REQ-022]
  - **Test AC**:
    - Given a lint+test block that always fails, when the retry wrapper runs, then exactly three attempts occur and no more.
    - Given all three attempts fail, when the wrapper exits, then a mock verifies the `human-review` label was applied to the target issue.

- [ ] **TRD-028**: Undo / rollback (3h) [satisfies REQ-019] [depends: TRD-023]
  - **Validates PRD ACs**: AC-019-1, AC-019-2, AC-019-3
  - **Implementation AC**:
    - Given `undo_last_change()` is called, when the tool inspects `HEAD`, then it verifies the commit author matches the configured identity before acting.
    - Given the current branch is the dynamically-detected default branch, when undo is attempted, then it is refused with `ON_DEFAULT_BRANCH_REFUSED`.

- [ ] **TRD-028-TEST**: Verify undo / rollback (2h) [verifies TRD-028] [satisfies REQ-019]
  - **Test AC**:
    - Given a commit authored by someone other than the configured identity, when undo runs, then the operation refuses with a structured error.
    - Given the checkout is on the default branch, when undo runs, then it is refused with `ON_DEFAULT_BRANCH_REFUSED` regardless of commit author.

### Cluster 6 — Polish (9h impl + 5h test)

- [ ] **TRD-029**: Serial queue processor (3h) [satisfies REQ-012, REQ-024] [depends: TRD-006, TRD-016]
  - **Validates PRD ACs**: AC-012-1, AC-012-2, AC-012-3
  - **Implementation AC**:
    - Given a queue of issues, when the orchestrator runs, then it processes one at a time to a terminal state, advancing only after completion; >10 items emits a warning.
    - Given a `NETWORK_ERROR` is raised mid-issue, when the orchestrator catches it, then state is preserved and processing pauses until the next successful tool call.

- [ ] **TRD-029-TEST**: Verify serial queue processor (2h) [verifies TRD-029] [satisfies REQ-012, REQ-024]
  - **Test AC**:
    - Given a queue of 3 issues, when the orchestrator runs with only one succeeding at a time, then issues terminate serially in order.
    - Given a mocked `NETWORK_ERROR` during issue processing, when the orchestrator handles it, then state is preserved and the queue resumes on the next successful call.

- [ ] **TRD-030**: Polling task (3h) [satisfies REQ-008] [depends: TRD-016, TRD-006]
  - **Validates PRD ACs**: AC-008-1, AC-008-2, AC-008-3
  - **Implementation AC**:
    - Given `issue_agent_start`, when the polling task is created, then it loops using `asyncio.create_task` and sleeps `poll_interval_min * 60` seconds between iterations.
    - Given a transient error during a poll, when the loop catches it, then the error is swallowed (logged) and the next tick continues normally.

- [ ] **TRD-030-TEST**: Verify polling task (1h) [verifies TRD-030] [satisfies REQ-008]
  - **Test AC**:
    - Given a polling interval of 5 minutes, when the task runs with mocked time, then each iteration sleeps 300s.
    - Given a transient exception on one iteration, when the task continues, then it does not crash and proceeds to the next tick.

- [ ] **TRD-031**: Network & rate-limit handling (2h) [satisfies REQ-024] [depends: TRD-015]
  - **Validates PRD ACs**: AC-024-1, AC-024-2
  - **Implementation AC**:
    - Given a rate-limit response, when the handler parses `X-RateLimit-Reset`, then it produces a human-readable reset time string.
    - Given a network error surface, when the handler classifies it, then it maps to the correct structured error code.

- [ ] **TRD-031-TEST**: Verify network & rate-limit handling (1h) [verifies TRD-031] [satisfies REQ-024]
  - **Test AC**:
    - Given a mocked rate-limit response with known reset epoch, when the handler parses it, then the resulting human-readable string matches the expected formatted time.
    - Given mocked connection errors, when the handler classifies them, then each maps to the correct structured error code.

- [ ] **TRD-032**: Naming helpers (1h) [satisfies REQ-021]
  - **Validates PRD ACs**: AC-021-1, AC-021-2
  - **Implementation AC**:
    - Given a title, when `slugify(title, max=40)` runs, then it produces a lowercase, dash-separated, ≤40-char slug.
    - Given an issue number and a title, when `branch_name`, `commit_msg`, and `pr_title` run, then they produce strings matching the documented conventions.

- [ ] **TRD-032-TEST**: Verify naming helpers (1h) [verifies TRD-032] [satisfies REQ-021]
  - **Test AC**:
    - Given sample titles with special characters and excess length, when `slugify` runs, then the output conforms to the length and character-set contract.
    - Given an issue number and title, when all four helpers run, then each result matches the documented pattern (branch, commit, PR title).

### Cluster 7 — Docs & Packaging (7h impl + 1.5h test)

- [ ] **TRD-033**: agent_protocol.md content (3h) [satisfies REQ-020, REQ-020b]
  - **Validates PRD ACs**: AC-020-1, AC-020-2, AC-020b-1, AC-020b-2
  - **Implementation AC**:
    - Given the agent protocol template, when authoring is complete, then it covers the full semi and full workflow text plus Step 0 conventions.
    - Given the template contains placeholder vars, when the renderer (TRD-013) substitutes them, then every placeholder is resolvable from the documented variable set.

- [ ] **TRD-033-TEST**: Verify agent_protocol.md content (1h) [verifies TRD-033] [satisfies REQ-020, REQ-020b]
  - **Test AC**:
    - Given the rendered template, when scanned for placeholders, then no unresolved `{{var}}` tokens remain.
    - Given the documented variable set, when a coverage audit runs, then every placeholder in the template is present in the variable set (and vice versa).

- [ ] **TRD-034**: README.md (3h) [satisfies REQ-001]
  - **Implementation AC**:
    - Given the PRD's required sections, when README.md is authored, then it contains all 8 sections (Install, Quick Start, Commands, Modes, Token howto, Security, Troubleshooting, and the remaining PRD-mandated section).
    - Given the README is written, when reviewed, then every section includes a working example or actionable instructions.

- [ ] **TRD-034-TEST**: Verify README.md (0.5h) [verifies TRD-034] [satisfies REQ-001]
  - **Test AC**:
    - Given the README is parsed by a markdown parser, when a section-presence audit runs, then all 8 required section headings are present.
    - Given code blocks in the README, when scanned, then each references a command or example that exists in the codebase.

- [ ] **TRD-035**: Packaging (1h) [satisfies INFRA]
  - **Implementation AC**:
    - Given the project root, when packaging is complete, then `pyproject.toml` declares the console entry-point, `.gitignore` is present, and `requirements.txt` is pinned.
    - Given the packaging files, when `pip install .` runs in a clean venv, then the entry-point command resolves.

### Infra

- [ ] **TRD-INFRA-01**: asyncio app composition root (2h) [satisfies ARCH]
  - **Implementation AC**:
    - Given `ghia/app.py`, when `create_app()` is invoked, then it wires FastMCP, Starlette, SessionStore, and the polling task into a single event loop composition.
    - Given `create_app()` returns, when the app is started, then all subsystems share the same event loop and no subsystem is implicitly started before activation.

- [ ] **TRD-INFRA-01-TEST**: Verify asyncio app composition root (1h) [verifies TRD-INFRA-01] [satisfies ARCH]
  - **Test AC**:
    - Given `create_app()` is called, when the returned app is inspected, then FastMCP, Starlette, and SessionStore handles are all reachable.
    - Given the app is started in a test harness, when it is inspected, then no polling task exists until activation occurs explicitly.

- [ ] **TRD-INFRA-02**: Playwright test harness (1h) [satisfies ARCH]
  - **Implementation AC**:
    - Given a developer runs the UI test setup, when the harness is installed, then Playwright and its browsers are available locally.
    - Given the harness provides a fixture launcher for `picker.html`, when a test imports it, then the fixture serves `picker.html` and exposes a Playwright page context.

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

## 5b. Recommended Team Assignments (advisory)

> **Auto-generated by `/ensemble:configure-team` on 2026-04-23.** This section is ADVISORY — the project has no local `packages/*/agents/` registry, so team-mode strict validation is skipped. Implementation runs in single-agent mode; at runtime, specialist work is delegated manually via the `Task` tool to the globally-installed `ensemble-full:*` agents listed below.

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
