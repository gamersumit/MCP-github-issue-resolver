"""Permission policy for the github-issue-agent's PreToolUse hook.

The single user-facing surface is :mod:`ghia.policy.permission_policy`,
which reads a Claude Code PreToolUse hook event on stdin and writes an
allow / deny / ask decision on stdout. The wizard wires it into the
target repo's ``.claude/settings.local.json`` so the agent can run
git / gh / build / test commands without the user clicking "approve"
on every step.
"""
