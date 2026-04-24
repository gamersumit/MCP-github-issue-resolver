#!/usr/bin/env bash
# github-issue-agent — one-command local installer (TRD-007).
#
# Creates a virtualenv under ./.venv, installs runtime + dev deps,
# runs the setup wizard (which auto-detects the repo from git remote
# get-url origin and reuses your existing `gh` CLI auth), and
# (best-effort) registers the MCP server with Claude Code.
#
# Safe to re-run: pip + venv are idempotent, and the wizard loads any
# existing per-repo config as defaults. We never push to remote or modify
# anything outside the repo directory + ~/.config/github-issue-agent/.
#
# Requirements: Python 3.10+, pip, git, and `gh` CLI on $PATH.
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

echo "Installing package in editable mode…"
pip install --quiet -e .

# ----------------------------------------------------------------------
# Setup wizard (interactive)
# ----------------------------------------------------------------------

echo ""
echo "Launching setup wizard…"
echo ""
python -m setup_wizard

# ----------------------------------------------------------------------
# Claude Code MCP registration (best-effort)
# ----------------------------------------------------------------------

VENV_PYTHON="$REPO_ROOT/.venv/bin/python"
if command -v claude >/dev/null 2>&1; then
    echo ""
    echo "Registering MCP server with Claude Code…"
    if claude mcp add github-issue-agent -- "$VENV_PYTHON" -m server; then
        echo "Registered with Claude Code."
    else
        echo "claude mcp add returned non-zero; you may already have it registered." >&2
        echo "Verify with: claude mcp list" >&2
    fi
else
    echo ""
    echo "Claude Code CLI not found on PATH — skipping auto-registration."
    echo "Register manually:"
    echo "  claude mcp add github-issue-agent -- $VENV_PYTHON -m server"
fi

echo ""
echo "Setup complete. In Claude Code, type /issue-agent start to begin."
