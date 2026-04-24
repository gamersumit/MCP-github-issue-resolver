"""TRD UX-fix: prompt registrations on the FastMCP server.

Why prompt-level tests (not just tool tests): Claude Code surfaces
``@mcp.prompt()`` registrations as literal slash commands of the form
``/mcp__github-issue-agent__<name>``. v0.1 advertised slash commands
that didn't exist (the readme said `/issue-agent start` but no prompt
was registered, so users got "Unknown command" in their first
session). These tests pin the registrations so a future refactor
can't silently strip them.

The prompt functions are deliberately thin — each returns a one-line
instruction telling the LLM to call the matching tool. We assert on
that string here; behavioural tool tests live in
``tests/test_control.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import server


# Why this discovery indirection: FastMCP's prompt store has changed
# attribute names across versions (``_prompts`` in older builds,
# ``_prompt_manager._prompts`` in 2.x). Centralizing the lookup in one
# helper keeps every test resilient and the failure message useful.
def _registered_prompts() -> dict[str, Any]:
    mgr = getattr(server.mcp, "_prompt_manager", None)
    assert mgr is not None, (
        "FastMCP changed its prompt-storage layout — update the discovery "
        "helper in this test."
    )
    return dict(mgr._prompts)  # type: ignore[attr-defined]


EXPECTED_PROMPTS = {"start", "stop", "status", "set_mode", "fetch_now"}


def test_all_five_prompts_registered() -> None:
    names = set(_registered_prompts().keys())
    missing = EXPECTED_PROMPTS - names
    assert not missing, f"missing prompts: {missing}; have: {sorted(names)}"


def _render(name: str, **kwargs: Any) -> str:
    """Invoke the underlying function of a registered prompt.

    FastMCP wraps the user function in a ``FunctionPrompt`` with the
    callable still reachable as ``.fn``. We bypass the full render
    pipeline (which requires a Context) because the assertions only
    care about the string the function returns.
    """

    prompt = _registered_prompts()[name]
    fn = getattr(prompt, "fn", None)
    assert fn is not None, (
        f"FunctionPrompt for {name!r} has no .fn attribute — FastMCP layout changed"
    )
    result = fn(**kwargs)
    if asyncio.iscoroutine(result):
        result = asyncio.get_event_loop().run_until_complete(result)
    return result  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    "prompt_name,tool_name",
    [
        ("start", "issue_agent_start"),
        ("stop", "issue_agent_stop"),
        ("status", "issue_agent_status"),
        ("fetch_now", "issue_agent_fetch_now"),
    ],
)
def test_passthrough_prompts_reference_matching_tool(
    prompt_name: str, tool_name: str
) -> None:
    """Each pass-through prompt must name the tool it shims.

    Why pin the tool name in the prompt's return string: the LLM uses
    the literal name as a routing hint. If a prompt returns "Call the
    fetch tool" instead of "Call the `issue_agent_fetch_now` tool now",
    the LLM may hallucinate the wrong tool name and silently fail.
    """

    output = _render(prompt_name)
    assert isinstance(output, str)
    assert tool_name in output


def test_set_mode_with_valid_mode_returns_call_instruction() -> None:
    out = _render("set_mode", mode="full")
    assert "issue_agent_set_mode" in out
    assert "full" in out


def test_set_mode_with_semi_works_too() -> None:
    out = _render("set_mode", mode="semi")
    assert "issue_agent_set_mode" in out
    assert "semi" in out


def test_set_mode_with_invalid_mode_returns_clarification_not_call() -> None:
    """Invalid mode → friendly clarification, not a tool-call instruction.

    The prompt-side validation lets the LLM correct the user before
    bouncing through the tool layer with a guaranteed-bad arg.
    """

    out = _render("set_mode", mode="invalid")
    assert "not a valid mode" in out
    assert "semi" in out and "full" in out
    # Crucially, must NOT instruct the LLM to call the tool.
    assert "Call `issue_agent_set_mode`" not in out


def test_set_mode_normalizes_case_and_whitespace() -> None:
    """`FULL ` and `Semi` should be accepted — match the tool's tolerance."""

    out_upper = _render("set_mode", mode="FULL")
    assert "full" in out_upper and "issue_agent_set_mode" in out_upper

    out_padded = _render("set_mode", mode="  semi  ")
    assert "semi" in out_padded and "issue_agent_set_mode" in out_padded
