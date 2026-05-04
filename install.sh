#!/usr/bin/env bash
# github-issue-agent — one-command local installer (TRD-007).
#
# This script does ONE machine-level job: install the package and register
# the MCP server at USER scope so it's available from every repo (not just
# this clone dir). It NEVER runs the per-repo setup wizard — that is a
# separate ceremony the user runs from inside whichever repo they want to
# manage with the agent (see the "Next steps" panel printed at the end).
#
# Why two ceremonies (install + per-repo wizard) instead of one:
#   v0.1 fused them and saved per-repo config FOR THE AGENT'S OWN CLONE
#   DIR — surprising and useless. Splitting them keeps install.sh as a
#   "once per machine" thing and the wizard as a "once per target repo"
#   thing, with no implicit cwd dependency between the two.
#
# Safe to re-run: pip + venv are idempotent, and we detect an existing
# Claude Code MCP registration before re-adding (silently skip on match,
# re-register at user scope if found at the wrong scope).
#
# Requirements: Python 3.10+, pip, git.
# Optional: claude CLI (for automatic MCP registration).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"

# ----------------------------------------------------------------------
# Preflight: Python 3.10+
# ----------------------------------------------------------------------

if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: python3 not on PATH. Install Python 3.10 or newer." >&2
    exit 1
fi

PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
case "$PYVER" in
    3.1[0-9]|3.[2-9][0-9]|[4-9].*)
        : # 3.10 .. 3.99, or 4.x+ — acceptable
        ;;
    *)
        echo "Error: Python 3.10+ required (found $PYVER)" >&2
        exit 1
        ;;
esac

echo "Using Python $PYVER from $(command -v python3)"

# ----------------------------------------------------------------------
# Virtualenv
# ----------------------------------------------------------------------

if [ ! -d ".venv" ]; then
    echo "Creating virtualenv at $REPO_ROOT/.venv"
    python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# ----------------------------------------------------------------------
# Dependencies
# ----------------------------------------------------------------------

echo "Upgrading pip…"
pip install --quiet --upgrade pip

echo "Installing runtime dependencies…"
pip install --quiet -r requirements.txt

# Editable install lands two console scripts in .venv/bin/:
#   github-issue-agent          (the MCP server entry point)
#   github-issue-agent-setup    (the per-repo wizard)
# But .venv/bin/ is NOT on the user's PATH, so neither script is callable
# from a target repo without activating the venv — defeating the point of
# the user-scope MCP design. We symlink both into ~/.local/bin/ below to
# make them globally callable.
echo "Installing package in editable mode…"
pip install --quiet -e .

# ----------------------------------------------------------------------
# Expose console scripts on the user's PATH
# ----------------------------------------------------------------------
#
# v0.2.0 fix: ~/.local/bin is the standard XDG user-scope bin dir and is
# already on PATH for the vast majority of Linux distros and recent macOS
# shells (per `man 7 file-hierarchy` + Ubuntu's default ~/.profile). We
# symlink so the user can run `github-issue-agent-setup` from inside ANY
# repo without remembering the venv path. Idempotent: -f overwrites stale
# symlinks (e.g. from a previous clone path) so re-running install.sh
# from a fresh clone Just Works.

LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$LOCAL_BIN"

echo ""
echo "Linking console scripts into $LOCAL_BIN…"
ln -sf "$REPO_ROOT/.venv/bin/github-issue-agent"       "$LOCAL_BIN/github-issue-agent"
ln -sf "$REPO_ROOT/.venv/bin/github-issue-agent-setup" "$LOCAL_BIN/github-issue-agent-setup"
echo "  $LOCAL_BIN/github-issue-agent        -> $REPO_ROOT/.venv/bin/github-issue-agent"
echo "  $LOCAL_BIN/github-issue-agent-setup  -> $REPO_ROOT/.venv/bin/github-issue-agent-setup"

# Warn (but don't fail) if ~/.local/bin isn't on PATH yet. This is the
# one-line fix the user needs in their shell rc, and is the only manual
# step left after install.sh runs.
case ":$PATH:" in
    *":$LOCAL_BIN:"*)
        : # already on PATH — nothing to do
        ;;
    *)
        echo ""
        echo "Note: $LOCAL_BIN is not on your PATH yet. Add this line to"
        echo "your shell rc (~/.bashrc or ~/.zshrc) and restart the shell:"
        echo ""
        echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
        echo ""
        ;;
esac

# ----------------------------------------------------------------------
# Claude Code MCP registration (best-effort, USER scope, idempotent)
# ----------------------------------------------------------------------
#
# Why USER scope (not the default "local"):
#   "local" only registers the MCP for the current cwd — meaning the
#   MCP would only work when Claude Code is launched from inside this
#   clone dir. The user wants to use the agent from any repo, so we
#   register at user scope (covers every project on the machine).
#
# Idempotency: `claude mcp list` exits 0 and prints registered servers.
# We grep for "github-issue-agent" — if found, we skip the add to keep
# re-runs quiet. If found at the wrong scope (caller passed the v0.1
# install which used the default local scope), we remove + re-add at
# user scope. `claude mcp remove` accepts -s <scope>, so we try "local"
# first (the most common stale scope) before re-adding.

VENV_PYTHON="$REPO_ROOT/.venv/bin/python"

if command -v claude >/dev/null 2>&1; then
    echo ""
    echo "Registering MCP server with Claude Code (user scope)…"

    # `claude mcp list` shows ALL scopes. If the agent is registered
    # anywhere, we don't want to blindly re-add (would double-register
    # or error depending on scope). Capture the output and inspect.
    if EXISTING=$(claude mcp list 2>/dev/null) && \
       echo "$EXISTING" | grep -q "github-issue-agent"; then
        # Already registered. We can't easily detect the scope from
        # `mcp list` output (format varies by claude-code version), so
        # we attempt a no-op user-scope re-register: remove from local
        # (silently — non-zero just means "wasn't there") then add at
        # user. If add fails because user-scope already has it, that's
        # the desired end state — surface and continue.
        echo "MCP server already registered. Ensuring user scope…"
        claude mcp remove github-issue-agent -s local >/dev/null 2>&1 || true
        if claude mcp add -s user github-issue-agent -- "$VENV_PYTHON" -m server >/dev/null 2>&1; then
            echo "Re-registered at user scope."
        else
            echo "Already at user scope (or another scope). Verify with: claude mcp list"
        fi
    else
        if claude mcp add -s user github-issue-agent -- "$VENV_PYTHON" -m server; then
            echo "Registered at user scope."
        else
            echo "claude mcp add returned non-zero — verify with: claude mcp list" >&2
            echo "Manual command:" >&2
            echo "  claude mcp add -s user github-issue-agent -- $VENV_PYTHON -m server" >&2
        fi
    fi
else
    echo ""
    echo "Claude Code CLI not found on PATH — skipping auto-registration."
    echo "Register manually after installing the claude CLI:"
    echo "  claude mcp add -s user github-issue-agent -- $VENV_PYTHON -m server"
fi

# ----------------------------------------------------------------------
# Next steps (replaces v0.1's "we just ran the wizard for you" surprise)
# ----------------------------------------------------------------------

cat <<'EOF'

----------------------------------------------------------------------
github-issue-agent installed (global MCP, user scope).

To use it on one of your repos:
  1. cd into the target repo:
       cd ~/path/to/your/repo
  2. Run the per-repo setup wizard:
       github-issue-agent-setup
  3. Open Claude Code in that repo:
       claude
  4. Start the agent (slash command or natural language):
       /mcp__github-issue-agent__start
       (or just say:  "start the issue agent")

The wizard auto-detects the repo + your active gh account.
Switch accounts with `gh auth switch -u <account>` before step 2.
----------------------------------------------------------------------
EOF
