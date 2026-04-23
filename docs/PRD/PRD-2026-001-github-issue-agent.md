---
Document ID: PRD-2026-001
Version: 1.1.0
Status: Ready for TRD
Date: 2026-04-23
Scale Depth: STANDARD
Total Requirements: 28
Readiness Score: 4.25 / 5.0 (PASS)
---

# PRD-2026-001 — GitHub Issue Agent MCP Server

## 1. PRD Health Summary

| Metric | Count |
|---|---|
| **Total Requirements** | 28 |
| **Must** | 25 |
| **Should** | 2 |
| **Could** | 1 |
| **Won't (this release)** | 8 (see Non-Goals) |
| **AC Coverage** | 28/28 requirements (100%) |
| **Risk-flagged Requirements** | 6 |
| **Cross-requirement Dependencies** | 18 |
| **Self-critique issues resolved** | 7 |

---

## 2. Product Summary

### 2.1 Problem Statement
Maintainers of GitHub repositories — solo open-source developers, small dev teams, library authors — accumulate issue backlogs faster than they can clear them. Routine bugs ("typo in docs", "this function crashes on empty input", "add missing null check") pile up, burning developer attention and discouraging contributors. Existing AI coding assistants (Claude Code, Cursor) require manual prompting for each issue, so automation is shallow and repetitive.

### 2.2 Solution Overview
**github-issue-agent** is a Model Context Protocol (MCP) server that installs into Claude Code and gives Claude direct tools to fetch GitHub issues, read and modify the local repository, run tests in a Docker sandbox, and open pull requests — all driven by a pre-written agent protocol injected on activation. The user operates it with two slash commands (`/issue-agent start`, `/issue-agent stop`) and a browser-based (or terminal) issue picker.

Two operating modes:
- **Semi-auto** — Claude proposes each step, user approves (default)
- **Full-auto** — Claude executes end-to-end and opens draft PRs for review

### 2.3 Value Proposition
Zero manual prompt engineering; the user knows only two commands; drafts PRs within minutes of install.

### 2.4 Target Users
- **Solo open-source maintainers** with growing issue backlogs
- **Small dev teams (2-5)** delegating routine bug fixes to AI
- **Library authors** receiving frequent, similar bug reports

---

## 3. User Analysis

### 3.1 Personas
**P1 — Solo Maintainer "Maya"**
- Runs a small Python library with 200+ stars, 30 open issues
- Wants to clear the "good first issue" / `ai-fix` backlog without context-switching
- Values: minimal ceremony, safe defaults, no surprises

**P2 — Team Lead "Dan"**
- Leads a 4-person team, triages issues weekly
- Labels routine bugs `ai-fix`, wants Claude to draft PRs his team reviews
- Values: reviewable diffs, linked PRs, no direct pushes to main

**P3 — Offline-first Developer "Priya"**
- Works from restricted or SSH'd-into dev environments
- No browser auto-open possible
- Values: terminal fallback, clear failure messages when offline

### 3.2 Pain Points
- Context-switching between issue tracker, editor, terminal for trivial fixes
- Writing custom prompts for each similar bug
- Fear of AI pushing broken code to main branch
- Token/credential management complexity

### 3.3 Success Metrics
| Metric | Target |
|---|---|
| Time from install → first PR opened | < 10 minutes |
| Commands user must memorize | ≤ 2 |
| Issues successfully resolved in full-auto mode | ≥ 80% of `ai-fix`-labeled issues |
| Manual prompt engineering required per issue | 0 |
| Setup wizard completion rate (first run) | ≥ 95% |

---

## 4. Goals and Non-Goals

### 4.1 Goals
- Ship a single-command install that registers the MCP with Claude Code
- Deliver two operational modes (semi / full) with clear safety differences
- Auto-inject an agent protocol so users never hand-craft prompts
- Provide a clean browser UI with a terminal fallback for headless environments
- Enforce safety defaults: draft PRs in full-auto, never push to main, atomic writes, Docker-sandboxed tests

### 4.2 Non-Goals (this release)
- Multi-repo support (single repo only)
- GitLab, Bitbucket, or self-hosted Git integration (GitHub only)
- PR review agent (Claude does not review its own PRs post-merge)
- Slack / Discord / email notifications
- Learning loop from merged vs. rejected PRs
- CI integration (waiting for GitHub Actions to pass before notifying)
- Issue complexity scoring before pick
- Windows-native support (Linux/macOS primary; Windows via WSL best-effort)

---

## 5. Requirements by Feature Area

### 5.1 Feature Area A — Installation & Setup

#### REQ-001: One-command install script
**Priority:** Must | **Complexity:** Medium

`install.sh` installs Python dependencies, scaffolds the project, runs the setup wizard, and registers the MCP with Claude Code — in a single pipe-to-bash invocation.

- **AC-001-1:** Given Python 3.10+ is installed, when user runs the install command, then dependencies install, wizard launches, and `claude mcp add github-issue-agent` completes successfully.
- **AC-001-2:** Given Python 3.10+ is NOT installed, when user runs the install command, then the script exits with a clear error naming the required version.
- **AC-001-3:** Given the Claude Code CLI is not installed, when the script tries to register the MCP, then it prints the manual registration command and exits gracefully (partial success).

#### REQ-002: Bulk-collect setup wizard
**Priority:** Must | **Complexity:** Medium

A one-pass interactive wizard (`setup.py`) collects ALL required config (GitHub token, repo, issue label, default mode, poll interval, git identity, test runner, linter) in a single smooth flow. Sensible defaults applied aggressively; user can press Enter to accept. Test runner and linter are **auto-detected** from repo files (`package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, `Gemfile`+`spec/`, `pom.xml`, `ruff.toml`, `.eslintrc`, `rubocop.yml`, etc.) and presented for one-tap confirmation — not as free-form questions. [RISK: UX — too many questions at once can feel overwhelming; mitigated by auto-detection, defaults, and grouping]

- **AC-002-1:** Given the wizard runs for the first time, when user completes all steps, then `config.json` is written with chmod 600 and includes: token, repo, label (default `ai-fix`), mode (default `semi`), poll_interval_min (default 30), test_command, lint_command.
- **AC-002-2:** Given user presses Enter without input, when a prompt has a default, then the default is accepted.
- **AC-002-3:** Given the wizard is re-run (via `/issue-agent setup`), when prior `config.json` exists, then current values are shown as defaults, user edits only what changes.
- **AC-002-4:** Given the repo contains `pyproject.toml`, when the wizard runs detection, then it prompts `Detected test runner: pytest. Use this? [Y/n/custom]` — not a free-form question.
- **AC-002-5:** Given no recognized test config file exists, when the wizard runs detection, then it asks the user to type a test command, then validates it against the allow-list from AC-017-3; invalid commands are rejected with the allow-list shown.

#### REQ-003: Token validation on setup
**Priority:** Must | **Complexity:** Low

The wizard validates the GitHub token immediately by calling `GET /user` and refuses to save config if the token is invalid or lacks required scopes. [RISK: fine-grained PAT may report scopes differently than classic PAT]

- **AC-003-1:** Given a valid token with required scopes, when wizard calls `GET /user`, then it prints `✅ Authenticated as {username}` and proceeds.
- **AC-003-2:** Given an invalid or revoked token, when wizard calls `GET /user`, then it prints a clear error and re-prompts the user.
- **AC-003-3:** Given a token missing required scopes (`repo`, `issues`, `pull_requests` or fine-grained equivalents), when wizard checks scopes, then it warns the user naming the missing scopes and asks whether to continue.

#### REQ-004: MCP registration with Claude Code
**Priority:** Must | **Complexity:** Low

Install script runs `claude mcp add github-issue-agent` with the correct entry command and working directory, so the MCP auto-loads in future Claude Code sessions.

- **AC-004-1:** Given Claude Code CLI is installed, when install script runs, then the MCP appears in `claude mcp list` output.
- **AC-004-2:** Given the MCP is registered, when user opens Claude Code in any directory, then `/issue-agent` slash commands are available.

---

### 5.2 Feature Area B — Agent Control & State

#### REQ-005: Idle-by-default with manual activation
**Priority:** Must | **Complexity:** Low

The agent does nothing on MCP load — no fetching, no polling, no context injection — until user explicitly calls `issue_agent_start`.

- **AC-005-1:** Given Claude Code starts a new session, when the MCP loads, then status is `idle` and no polling timer is active.
- **AC-005-2:** Given status is `idle`, when user types unrelated commands (e.g., `/help`), then no agent-specific context is injected.

#### REQ-006: Persistent session state
**Priority:** Must | **Complexity:** Medium

Session state (status, mode, queue, active issue, completed list, skipped list, timestamps) is persisted to `state/session.json` after every state transition so work resumes cleanly across Claude Code restarts.

- **AC-006-1:** Given an active session with 3 queued issues, when Claude Code is closed and reopened and `issue_agent_status` is called, then the queue, active issue, and completed list are restored.
- **AC-006-2:** Given a write to `state/session.json` fails (disk full, permission), when the write is attempted, then the tool returns a structured error and the in-memory state is preserved.
- **AC-006-3:** Given `state/session.json` is corrupted on load, when the MCP starts, then it logs the corruption, moves the bad file to `session.json.bak-{timestamp}`, and starts with a clean idle state.

#### REQ-007: Semi-auto and Full-auto modes
**Priority:** Must | **Complexity:** Medium

Two distinct operating modes — `semi` pauses for user approval at defined checkpoints; `full` executes end-to-end and opens draft PRs. Mode is switchable mid-session via `issue_agent_set_mode`.

- **AC-007-1:** Given mode is `semi`, when Claude begins an issue, then it prompts the user at each checkpoint defined in the agent protocol before acting.
- **AC-007-2:** Given mode is `full`, when Claude begins an issue, then it executes all steps without prompting and opens the resulting PR as a **draft**.
- **AC-007-3:** Given mode is switched mid-session, when `issue_agent_set_mode` is called, then the change takes effect **immediately**: in-flight work (branch, edits) is preserved, but remaining steps for the active issue adopt the new mode.
- **AC-007-4:** Given mode switches from `full` → `semi` mid-issue, when the next checkpoint is reached, then Claude pauses and prompts the user even though it would not have done so moments earlier.
- **AC-007-5:** Given mode switches from `semi` → `full` mid-issue, when the next prompt would have been shown, then Claude skips it and proceeds end-to-end.

#### REQ-008: Polling timer for new issues
**Priority:** Must | **Complexity:** Medium

While active, the MCP polls GitHub at the configured interval (default 30 min) for new issues matching the label. New issues append to the queue; user is notified.

- **AC-008-1:** Given agent is active and poll interval is 30 min, when 30 min elapses, then `list_issues` is called and any new issues are appended to the session queue.
- **AC-008-2:** Given polling is active, when `issue_agent_stop` is called, then the timer is cancelled and no further network calls are made.
- **AC-008-3:** Given a polling call fails (network down, rate limit), when the error occurs, then the timer continues and the next poll is attempted; errors are logged, not surfaced each tick.

#### REQ-009: Slash command aliases
**Priority:** Must | **Complexity:** Low

The seven slash commands (`start`, `stop`, `status`, `auto`, `manual`, `fetch`, `setup`) map to MCP tool calls and are discoverable via `/issue-agent help`.

- **AC-009-1:** Given the MCP is registered, when user types `/issue-agent start`, then `issue_agent_start` is invoked.
- **AC-009-2:** Given user types `/issue-agent help` (or just `/issue-agent` with no arg), then a command reference table is printed.

---

### 5.3 Feature Area C — Issue Management

#### REQ-010: List and fetch issues by label
**Priority:** Must | **Complexity:** Low

`list_issues` returns open issues matching the configured label with priority inferred from labels (e.g., `priority:high`, `p0`), author, age, labels, URL, and a 200-char body summary.

- **AC-010-1:** Given the configured label is `ai-fix` and the repo has 5 such issues, when `list_issues` is called, then exactly those 5 are returned with the specified metadata.
- **AC-010-2:** Given an issue has no priority-indicating label, when metadata is built, then priority defaults to `medium`.

#### REQ-011: Browser UI issue picker with terminal fallback
**Priority:** Must | **Complexity:** High

A self-contained HTML picker served on `localhost:4242` lets users select issues, filter/search, and toggle mode. If the browser cannot be auto-opened (headless, SSH, no DISPLAY), a terminal-based picker (using `rich.prompt` or similar) is used instead. [RISK: dual UI paths double the maintenance burden — mitigate by sharing the same JSON data contract]

- **AC-011-1:** Given agent is activated on a machine with a working browser, when `issue_agent_start` runs, then `localhost:4242` opens automatically showing all fetched issues.
- **AC-011-2:** Given the browser auto-open fails or no DISPLAY is set, when `issue_agent_start` runs, then the terminal picker is launched and displays the same issue data.
- **AC-011-3:** Given the picker UI is open, when user searches "typo", then only issues with "typo" in title or number are shown.
- **AC-011-4:** Given the picker UI is open, when user presses Space on a card, then selection toggles; Enter confirms; Escape cancels.
- **AC-011-5:** Given the picker is open on a mobile-width screen, when rendered, then cards reflow to a single column and remain tappable.
- **AC-011-6:** Given user clicks "Start Working", when the POST to `/api/confirm` succeeds, then the tab auto-closes and the queue is persisted to state.

#### REQ-012: Issue queue with serial processing
**Priority:** Must | **Complexity:** Medium

Claude processes issues one at a time. Remaining issues wait in the queue. Queue persists across restarts (per REQ-006). Soft warning (not hard cap) when queue > 10 issues.

- **AC-012-1:** Given user picks 5 issues, when Claude begins work, then exactly one issue is `active_issue` at a time; others remain in `queue`.
- **AC-012-2:** Given the active issue completes (merged-ready PR or skipped), when Claude moves on, then the next issue in the queue becomes active.
- **AC-012-3:** Given user picks 15 issues, when the queue is persisted, then a warning is surfaced: "Large queue (15 issues) — this may take several hours. Continue?"

#### REQ-013: Post progress comments to issues
**Priority:** Must | **Complexity:** Low

Claude posts comments on the GitHub issue when it starts work and when the PR is opened, giving watchers visibility.

- **AC-013-1:** Given Claude begins an issue, when it creates the branch, then a comment "🤖 Working on this fix now. Branch: `fix/issue-{n}-{slug}`" is posted.
- **AC-013-2:** Given Claude opens a PR for the issue, when the PR is created, then a comment "🤖 Opened PR #{pr}: {url}" is posted.

#### REQ-013b: Duplicate-PR / duplicate-branch detection
**Priority:** Must | **Complexity:** Low

Before picking up an issue, Claude calls `check_issue_has_open_pr(issue_number)` to detect whether an open PR already references the issue (via `Closes #{n}` / `Fixes #{n}` / `Resolves #{n}` in the body) **or** a branch matching `fix/issue-{n}-*` already exists locally or on the remote. If a duplicate is detected, **warn the user and ask whether to proceed** — do not auto-skip.

- **AC-013b-1:** Given issue #42 has an open PR whose body contains "Closes #42", when the agent begins work on #42, then Claude surfaces `⚠️ PR #{pr_number} already addresses this issue ({pr_url}, author: {author}). Proceed anyway, skip, or close existing PR?` and waits for user choice.
- **AC-013b-2:** Given no duplicate PR or branch exists, when the check runs, then it returns `{has_open_pr: false}` and the agent proceeds normally.
- **AC-013b-3:** Given a local branch `fix/issue-42-parser-null` exists from a prior session but no open PR, when the check runs, then Claude warns "Local branch already exists for this issue — reuse, rename, or abandon?" and waits for user choice.

---

### 5.4 Feature Area D — Code & Git Operations

#### REQ-014: Filesystem tools with path traversal protection
**Priority:** Must | **Complexity:** Medium

`read_file`, `write_file`, `list_directory`, `search_codebase`, `get_repo_structure`, `read_multiple_files` all operate within the repository root. Any path resolving outside the repo root (via `..`, symlinks, absolute paths) is rejected with `PATH_TRAVERSAL` error. [RISK: symlinks pointing outside repo must be explicitly resolved and checked]

- **AC-014-1:** Given a repo root `/home/user/myrepo`, when `write_file` is called with path `../../etc/passwd`, then it returns `{success: false, code: "PATH_TRAVERSAL"}` without writing.
- **AC-014-2:** Given a file `secrets.txt` inside the repo, when `read_file` is called with relative path `secrets.txt`, then the contents are returned.
- **AC-014-3:** Given a symlink inside the repo pointing outside the repo, when `write_file` follows it, then the write is rejected with `PATH_TRAVERSAL`.

#### REQ-015: Atomic file writes
**Priority:** Must | **Complexity:** Medium

`write_file` writes to a temp file in the same directory and atomically renames to the target, so partial writes (disk full, crash mid-write) never corrupt the target file.

- **AC-015-1:** Given `write_file` is called and the disk has space, when the write completes, then the target file contains exactly the new content.
- **AC-015-2:** Given `write_file` is called and the write is interrupted (simulated crash before rename), when the process restarts, then the original target file is unchanged.

#### REQ-016: Git operations via native CLI with dynamic default-branch detection
**Priority:** Must | **Complexity:** Low

`create_branch`, `git_diff`, `commit_changes`, `push_branch`, `get_current_branch` invoke the native `git` CLI in the repo directory. GitPython is an optional fallback. All operations honor the repo's existing git config.

**Every issue works on a new `fix/issue-{n}-{slug}` branch** — Claude never commits directly on any default-like branch. The default branch is **dynamically detected** via `git symbolic-ref refs/remotes/origin/HEAD`; if that fails, Claude probes `main`, `master`, `trunk`, `develop` in order and uses the first one that exists. Result is cached in session state. If no default branch can be detected, all destructive ops refuse with `NO_DEFAULT_BRANCH_DETECTED`.

- **AC-016-1:** Given `git` is on PATH, when `create_branch("fix/issue-42-foo")` is called, then `git checkout -b fix/issue-42-foo` runs against the detected default branch base and returns success.
- **AC-016-2:** Given a branch name already exists, when `create_branch` is called, then the name is suffixed with `-v2`, `-v3`, etc., until unique.
- **AC-016-3:** Given there are uncommitted changes, when `commit_changes(msg)` is called, then `git add -A && git commit -m msg` runs and the commit SHA is returned.
- **AC-016-4:** Given `git` is not on PATH, when a git tool is called, then it returns `{success: false, code: "GIT_NOT_FOUND"}`.
- **AC-016-5:** Given a repo whose default branch is `develop`, when the MCP starts, then `default_branch` in session state is set to `develop` (not hardcoded `main`).
- **AC-016-6:** Given the current branch IS the detected default branch, when any write tool (`commit_changes`, `push_branch`) is called, then it refuses with `ON_DEFAULT_BRANCH_REFUSED`.

#### REQ-017: Docker sandbox for test execution
**Priority:** Must | **Complexity:** High

`run_tests` runs the repo's configured test command inside a Docker container. The repo is mounted read-only except for a dedicated test output volume. The test command comes from config (e.g., `pytest`, `npm test`) — never arbitrary user-supplied shell. [RISK: Docker unavailable in some environments — must fail cleanly with guidance]

- **AC-017-1:** Given Docker is installed and configured, when `run_tests()` is called, then the configured test command runs in a container and structured results `{passed, failed, errors, output}` are returned.
- **AC-017-2:** Given Docker is not installed, when `run_tests()` is called, then it returns `{success: false, code: "DOCKER_UNAVAILABLE"}` with a link to install docs.
- **AC-017-3:** Given a user attempts to pass arbitrary shell in the test command config, when the MCP validates config, then only commands matching an allow-list regex (`^(pytest|npm|jest|go|cargo|mvn|gradle|ruby|rake|bundle)\b[\w\s\-=./]*$`) are accepted.
- **AC-017-4:** Given tests run inside the container, when the test process attempts to write outside the mounted output volume, then the write fails (read-only mount).

#### REQ-017b: Linting tool
**Priority:** Must | **Complexity:** Low

A `check_linting` tool invokes the repo's configured linter (`ruff`, `eslint`, `rubocop`, `golangci-lint`, etc.) on changed files only, returning structured results. The agent protocol requires running `check_linting` **before** `run_tests` in both modes — it's a cheap early signal and catches style/syntax regressions before the expensive test run. Commands are allow-list-validated the same way as test commands (AC-017-3). If no linter is detected or configured, the tool returns `{success: true, skipped: true, reason: "no linter configured"}`.

- **AC-017b-1:** Given a repo with `ruff.toml` and changed files in the current diff, when `check_linting()` is called, then `ruff check` runs against only the changed files and returns `{passed: int, issues: [{file, line, rule, message}]}`.
- **AC-017b-2:** Given no linter is configured in `config.json` and no recognized linter config file is present, when `check_linting()` is called, then the tool returns `{success: true, skipped: true, reason: "no linter configured"}` — not an error.
- **AC-017b-3:** Given the agent protocol is followed, when an issue is being worked, then `check_linting` runs before `run_tests` and lint failures are surfaced to the user (semi) or counted against the retry budget (full).

#### REQ-018: Draft PRs in full-auto, never auto-merge
**Priority:** Must | **Complexity:** Low

In full-auto mode, `create_pr` opens PRs with `draft=true`. The MCP exposes **no** merge tool — merging is always a human click on GitHub. In semi-auto mode, draft defaults to `false` (regular PR), but user can override.

- **AC-018-1:** Given mode is `full`, when `create_pr` is called without explicit `draft` arg, then the PR is opened as draft.
- **AC-018-2:** Given any mode, when a user tool call requests PR merge, then no such tool exists in the MCP registry and the operation is impossible.
- **AC-018-3:** Given `create_pr` body is built, when the PR is created, then the body contains the line `Closes #{issue_number}`.

#### REQ-019: Undo / rollback tool
**Priority:** Should | **Complexity:** Medium

An `undo_last_change` tool resets the current branch to the state before Claude's last commit (via `git reset --hard HEAD~1` when the last commit is Claude's). Never runs when HEAD is on `main`/`master`. [RISK: destructive — must refuse if branch has been pushed or has manual commits]

- **AC-019-1:** Given Claude made the last commit on a `fix/` branch and nothing is pushed, when `undo_last_change` is called, then the commit is removed and working tree is reset.
- **AC-019-2:** Given the last commit was made by a human (not Claude's configured signature), when `undo_last_change` is called, then it refuses with `UNDO_REFUSED_NOT_OURS`.
- **AC-019-3:** Given the current branch matches the **dynamically-detected** default branch (per REQ-016), when `undo_last_change` is called, then it refuses with `UNDO_REFUSED_PROTECTED_BRANCH`.

---

### 5.5 Feature Area E — Agent Protocol & Workflow

#### REQ-020: Auto-inject agent protocol on start
**Priority:** Must | **Complexity:** Low

When `issue_agent_start` is called, the contents of `prompts/agent_protocol.md` (with `{repo_name}`, `{mode}`, `{timestamp}`, `{issue_list}` placeholders substituted) are returned as part of the tool response so Claude has the workflow rules in-context.

- **AC-020-1:** Given `issue_agent_start` is called, when the response is returned, then it contains the populated protocol text under a `protocol` key.
- **AC-020-2:** Given the mode is `full`, when the protocol is rendered, then only the full-auto workflow section appears (semi section omitted to reduce confusion).

#### REQ-020b: Convention awareness (repo conventions discovery)
**Priority:** Must | **Complexity:** Low

On session start (triggered by `issue_agent_start`), Claude is instructed (via the injected protocol) to read any of the following files that exist and summarize the conventions discovered before starting the queue: `CLAUDE.md`, `CONTRIBUTING.md`, `AGENTS.md`, `.cursor/rules/*.md`, `.editorconfig`, and the top-level `README.md`. The summary is cached in session state (`discovered_conventions`) so it applies across all issues in the session without re-reading per issue.

- **AC-020b-1:** Given a repo with `CLAUDE.md` and `CONTRIBUTING.md`, when `issue_agent_start` is called, then the injected protocol contains a "Step 0" directing Claude to read both files and output a 3-5 bullet convention summary before picking the first issue.
- **AC-020b-2:** Given no convention files exist, when the protocol is executed, then Step 0 is a no-op and Claude proceeds directly to the first issue.
- **AC-020b-3:** Given conventions were discovered in session start, when a subsequent issue is worked in the same session, then the cached summary is referenced rather than re-reading the files.

#### REQ-021: Branch / commit / PR naming conventions
**Priority:** Must | **Complexity:** Low

Enforced by the agent protocol and by server-side validation in `create_branch`, `commit_changes`, `create_pr`:
- Branch: `fix/issue-{number}-{short-slug}` (slug is kebab-case, max 40 chars)
- Commit: `fix: {short description} (closes #{number})`
- PR title: `Fix: {issue title} (#{number})`

- **AC-021-1:** Given issue #42 titled "Null pointer in parser", when a branch is created, then its name starts with `fix/issue-42-` and ends with a kebab-slug of the title.
- **AC-021-2:** Given a commit message is built for issue #42, when committed, then the message ends with `(closes #42)`.

#### REQ-022: Max 3 retries per issue
**Priority:** Must | **Complexity:** Low

In full-auto, if tests fail, Claude may retry up to 2 additional times (3 total attempts). After 3 failures, the issue is flagged `human-review` (label added to the issue) and the queue moves on.

- **AC-022-1:** Given full-auto and an issue whose tests fail 3 consecutive times, when the 3rd attempt fails, then the issue is labeled `human-review` on GitHub and moved to `skipped` in session state.
- **AC-022-2:** Given semi-auto, when tests fail, then the user is asked how to proceed (no automatic retry).

---

### 5.6 Feature Area F — Non-Functional Requirements

#### REQ-023: Security — token handling
**Priority:** Must | **Complexity:** Medium

- `config.json` stored at `~/.config/github-issue-agent/config.json` with chmod 600
- Token is never printed to stdout/stderr, never logged, and is redacted from all error messages using **two layers**:
  1. **Literal replacement** (primary defense): the exact token string loaded from `config.json` is string-replaced with `***REDACTED***` in every outgoing log/error message.
  2. **Regex safety net**: `(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{20,255}` — covers classic PATs, fine-grained PATs (`github_pat_`), OAuth user-access, user-to-server, server-to-server, and refresh tokens.
- On MCP startup, token validity is verified via `GET /user`; if invalid, the server refuses to handle tool calls and instructs the user to re-run setup
- Fine-grained PAT with single-repo scope is recommended in wizard output

- **AC-023-1:** Given any tool call fails and raises an exception, when the error message is constructed, then neither the literal token string nor any substring matching the redaction regex appears in the output.
- **AC-023-2:** Given the token has been revoked, when the MCP starts, then all tool calls return `{success: false, code: "TOKEN_INVALID"}` with a message to re-run setup.
- **AC-023-3:** Given `config.json` is created, when the file is written, then `ls -l` shows mode `-rw-------` (600).
- **AC-023-4:** Given a fine-grained PAT (`github_pat_...`) is configured, when any log or error path is exercised, then the token is redacted — the redaction must not be specific to the classic `ghp_` prefix.

#### REQ-024: Reliability — graceful network degradation
**Priority:** Must | **Complexity:** Medium

If GitHub API is unreachable (network down, rate-limited, GitHub outage) mid-session, the active issue is paused, session state is preserved, user is shown a clear message, and the agent resumes automatically when network is restored (via the polling timer or on next tool call).

- **AC-024-1:** Given an active session and network drops mid-PR-creation, when the API call fails, then the tool returns `{success: false, code: "NETWORK_ERROR"}` with a human-readable message, and session state remains intact.
- **AC-024-2:** Given rate limit is hit, when the tool detects a 403 with `X-RateLimit-Remaining: 0`, then the error includes the reset timestamp in human-readable form.

#### REQ-025: Observability — structured error responses
**Priority:** Must | **Complexity:** Low

Every tool returns either `{success: true, data: ...}` or `{success: false, error: "...", code: "..."}`. Error codes come from a fixed enum: `TOKEN_INVALID`, `REPO_NOT_FOUND`, `FILE_NOT_FOUND`, `PATH_TRAVERSAL`, `TEST_FAILED`, `DOCKER_UNAVAILABLE`, `GIT_ERROR`, `GIT_NOT_FOUND`, `NETWORK_ERROR`, `RATE_LIMITED`, `BRANCH_EXISTS`, `PR_EXISTS`, `UNDO_REFUSED_NOT_OURS`, `UNDO_REFUSED_PROTECTED_BRANCH`, `CONFIG_MISSING`, `INVALID_INPUT`.

- **AC-025-1:** Given any tool raises an uncaught exception, when the MCP wrapper catches it, then the response is structured (never a raw stack trace to the client).
- **AC-025-2:** Given a tool returns a failure, when the error code is inspected, then it is one of the 16 defined codes — no ad-hoc strings.

---

## 6. Acceptance Criteria Summary Table

| REQ | Description | Priority | Complexity | AC Count |
|---|---|---|---|---|
| REQ-001 | One-command install | Must | Medium | 3 |
| REQ-002 | Bulk setup wizard (w/ test-runner & linter auto-detect) | Must | Medium | 5 |
| REQ-003 | Token validation on setup | Must | Low | 3 |
| REQ-004 | MCP registration | Must | Low | 2 |
| REQ-005 | Idle-by-default | Must | Low | 2 |
| REQ-006 | Persistent session state | Must | Medium | 3 |
| REQ-007 | Semi / full-auto modes (immediate switch) | Must | Medium | 5 |
| REQ-008 | Polling timer | Must | Medium | 3 |
| REQ-009 | Slash command aliases | Must | Low | 2 |
| REQ-010 | List/fetch issues by label | Must | Low | 2 |
| REQ-011 | Browser UI + terminal fallback picker | Must | High | 6 |
| REQ-012 | Serial queue | Must | Medium | 3 |
| REQ-013 | Post progress comments | Must | Low | 2 |
| REQ-013b | **Duplicate-PR / duplicate-branch detection** | Must | Low | 3 |
| REQ-014 | Path-traversal protection | Must | Medium | 3 |
| REQ-015 | Atomic file writes | Must | Medium | 2 |
| REQ-016 | Git ops + dynamic default-branch detection | Must | Low | 6 |
| REQ-017 | Docker sandbox for tests | Must | High | 4 |
| REQ-017b | **Linting tool** | Must | Low | 3 |
| REQ-018 | Draft PRs, never auto-merge | Must | Low | 3 |
| REQ-019 | Undo / rollback tool | **Should** | Medium | 3 |
| REQ-020 | Auto-inject agent protocol | Must | Low | 2 |
| REQ-020b | **Convention awareness (CLAUDE.md, etc.)** | Must | Low | 3 |
| REQ-021 | Branch/commit/PR naming | Must | Low | 2 |
| REQ-022 | Max 3 retries per issue | Must | Low | 2 |
| REQ-023 | Security — token handling (multi-prefix redaction) | Must | Medium | 4 |
| REQ-024 | Graceful network degradation | Must | Medium | 2 |
| REQ-025 | Structured error responses | Must | Low | 2 |

Total ACs: **85** across 28 requirements.

---

## 7. Dependency Map

| REQ | Depends On | Blocked By | Notes |
|---|---|---|---|
| REQ-002 (wizard) | REQ-003 (token validation), REQ-017 (for test-runner detection) | — | Wizard cannot save without valid token; uses allow-list from REQ-017 |
| REQ-004 (MCP registration) | REQ-001 (install script) | — | Registration is the final install step |
| REQ-006 (state persistence) | — | — | Foundational; used by REQ-007, REQ-008, REQ-012, REQ-016, REQ-020b |
| REQ-007 (modes — immediate switch) | REQ-006, REQ-020 | — | Mode affects protocol rendering and runtime behavior |
| REQ-008 (polling) | REQ-006, REQ-010 | — | Polling uses list_issues, mutates state |
| REQ-011 (UI picker) | REQ-010 (list_issues) | — | UI renders data from list_issues |
| REQ-012 (serial queue) | REQ-006 | — | Queue is part of persisted state |
| REQ-013b (duplicate PR detection) | REQ-010, REQ-016 | — | Uses GitHub API + local branch check |
| REQ-015 (atomic writes) | — | — | Foundational for REQ-014 |
| REQ-014 (path traversal) | REQ-015 | — | All write paths go through atomic writer |
| REQ-016 (git ops + default-branch detection) | — | — | Foundation for REQ-017, REQ-018, REQ-019, REQ-013b |
| REQ-017 (Docker tests) | REQ-016 | — | Tests run after commit_changes readiness |
| REQ-017b (linting) | REQ-016 | REQ-017b runs before REQ-017 in the protocol | |
| REQ-018 (draft PRs) | REQ-016 (git ops) | — | PR creation needs branch ready |
| REQ-019 (undo) | REQ-016 | — | Uses dynamically-detected default branch |
| REQ-020 (inject protocol) | — | — | Triggered by issue_agent_start |
| REQ-020b (conventions) | REQ-020 | — | Extends injected protocol with Step 0 |
| REQ-022 (3-retry limit) | REQ-017, REQ-017b | — | Retry budget consumed by lint + test failures |
| REQ-023 (token security) | REQ-003 | — | Validation uses redaction rules |
| REQ-024 (network degradation) | REQ-025 | — | Network errors use structured codes |

### Implementation Clusters (suggested build order)
1. **Cluster 1 — Foundation**: REQ-023 → REQ-015 → REQ-014 → REQ-025 → REQ-006
2. **Cluster 2 — Setup**: REQ-001 → REQ-003 → REQ-002 → REQ-004
3. **Cluster 3 — Core control**: REQ-005 → REQ-009 → REQ-020 → REQ-020b
4. **Cluster 4 — Issue ops**: REQ-010 → REQ-013 → REQ-013b → REQ-011
5. **Cluster 5 — Code ops**: REQ-016 → REQ-018 → REQ-017b → REQ-017 → REQ-022
6. **Cluster 6 — Polish**: REQ-007 → REQ-012 → REQ-008 → REQ-021 → REQ-024
7. **Cluster 7 — Nice-to-have**: REQ-019

---

## 8. Readiness Scorecard

Run after 7-issue self-critique pass (2026-04-23).

| Dimension | Score (1-5) | Rationale |
|---|---|---|
| **Completeness** | 4.5 | All 7 self-critique gaps closed (linting, duplicate detection, mode-switch semantics, token redaction, default-branch dynamics, wizard auto-detection, convention awareness). Feature areas A–F each cover their surface. Minor: no explicit telemetry/observability beyond structured errors — acceptable for solo MVP. |
| **Testability** | 4.5 | 85 ACs in Given/When/Then across 28 requirements; each Must has ≥ 2 ACs. Security ACs are regex-checkable. Only soft spot: AC-012-3 "warning surfaced" — subjective, but acceptable. |
| **Clarity** | 4.0 | Ambiguities from draft (mode-switch mid-issue, hardcoded default branch, redaction prefixes) resolved. Remaining mild ambiguity: exact UX of the terminal fallback picker (REQ-011) is under-specified — belongs in TRD, not PRD. |
| **Feasibility** | 4.0 | All requirements technically achievable with stated stack. Docker dependency (REQ-017) is the main risk but explicitly flagged and has graceful-fail path. Full-auto mode's 80% success metric is ambitious; retry cap + draft PRs mitigate blast radius. Solo build timeline not specified — can't fail feasibility on that alone. |
| **Overall** | **4.25** | **PASS** (threshold 4.0) |

**Gate Decision**: ✅ **PASS** — PRD is ready for TRD handoff. Suggested next step: `/ensemble:create-trd docs/PRD/PRD-2026-001-github-issue-agent.md`

---

## 9. Open Risks

- **R1**: "New idea" — no evaluation of existing products (Sweep AI, Aider, Devin, Copilot Workspace). Possible unknown overlap; consider a brief competitor scan post-MVP.
- **R2**: Docker availability is not universal on dev machines. REQ-017 failure path must be graceful and document native-test fallback as a follow-up.
- **R3**: Fine-grained PAT scope discovery is not standardized; REQ-003 AC-003-3 may produce noisy warnings.
- **R4**: Full-auto mode has non-trivial failure modes (tests flaky, Claude misunderstands issue); REQ-018 (draft PR) + REQ-022 (retry cap) are the two primary mitigations.
- **R5**: Dual UI paths (browser + terminal) in REQ-011 double test surface; shared data contract mitigates but both paths need E2E coverage.
- **R6**: Windows users (non-WSL) are unsupported this release; call this out in README explicitly.
