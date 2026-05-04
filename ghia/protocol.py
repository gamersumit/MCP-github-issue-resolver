"""Agent-protocol renderer (TRD-013).

Loads ``prompts/agent_protocol.md`` and produces the final prompt string
that ``issue_agent_start`` injects into Claude's context.

The template uses two kinds of markers:

* **Render-time variables** — single-brace ``{repo}``, ``{timestamp}``,
  ``{mode}``, ``{default_branch}``, ``{discovered_conventions}``,
  ``{issue_list}``.  These are the only variables this module
  substitutes.  Any other single-brace token (e.g. ``{number}``,
  ``{short-slug}``, ``{issue_title}``) is left verbatim because it is
  meant for Claude to interpret at agent runtime.
* **Conditional blocks** — ``{% if mode == "semi" %}...{% endif %}`` and
  ``{% if mode == "full" %}...{% endif %}``.  The template escapes the
  braces as ``{{% ... %}}`` so they are safe to keep inside an f-string
  or ``str.format`` call; we unescape them before parsing.  We
  implement the tiny subset of Jinja we need rather than pulling in a
  dependency.

Satisfies REQ-020.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

__all__ = [
    "ProtocolTemplateError",
    "render_protocol",
    "format_queue_summary",
    "template_path",
]


class ProtocolTemplateError(Exception):
    """Raised when the protocol template is missing or malformed.

    We surface a clear error rather than silently rendering an empty
    string: a missing template almost always means a packaging bug, and
    we want tests and operators to hear about it loudly.
    """


# The template ships inside the package at ``ghia/prompts/agent_protocol.md``.
# Keeping the asset under the package tree (and declaring it as
# package-data in ``pyproject.toml``) is what guarantees ``pip install .``
# embeds the file in the wheel — earlier layouts kept it at
# ``<repo_root>/prompts/...`` which silently dropped from wheel installs
# and broke ``issue_agent_start`` with ``FileNotFoundError``.
_PACKAGE_TEMPLATE_SUBPATH = Path("prompts") / "agent_protocol.md"
# Legacy repo-root path retained as a fallback so an editable install
# whose working tree predates the move still resolves the template.
_REPO_TEMPLATE_SUBPATH = Path("prompts") / "agent_protocol.md"


# Conditional block syntax — the template escapes the opening brace so
# the file is safe to pipe through ``str.format``; we unescape before
# parsing.  Matches ``{% if mode == "<arm>" %} ... {% endif %}`` with a
# non-greedy body.  DOTALL so the body may span many lines.
_BLOCK_RE = re.compile(
    r"\{%\s*if\s+mode\s*==\s*\"(?P<arm>semi|full)\"\s*%\}"
    r"(?P<body>.*?)"
    r"\{%\s*endif\s*%\}",
    re.DOTALL,
)


def template_path() -> Path:
    """Return the absolute path of the shipped protocol template.

    Resolution order:

    1. **Package-internal path** (``<pkg>/prompts/agent_protocol.md``).
       This is the canonical location post-packaging-fix: the asset
       lives inside the ``ghia`` package and ships with the wheel via
       the ``[tool.setuptools.package-data]`` declaration.  Wheel
       installs hit this branch.
    2. **Legacy repo-root path** (``<repo_root>/prompts/...``).  Kept
       so a developer who pulls a stale checkout — or an editable
       install whose working tree still has the old layout — keeps
       working without a re-install.

    The first existing candidate wins.  If neither exists the
    package-internal candidate is returned so callers get a clear
    ``FileNotFoundError`` pointing at the install-correct location
    rather than the legacy one.
    """

    pkg_dir = Path(__file__).resolve().parent
    candidates = [
        pkg_dir / _PACKAGE_TEMPLATE_SUBPATH,        # <pkg>/prompts/... (canonical)
        pkg_dir.parent / _REPO_TEMPLATE_SUBPATH,    # <repo>/prompts/... (legacy)
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def _unescape_jinja_braces(text: str) -> str:
    """Turn ``{{% ... %}}`` into ``{% ... %}`` for block parsing.

    The template doubles the braces so that, in principle, the raw
    template text can be passed through ``str.format`` without the
    block syntax tripping it up.  We undo that here because our block
    parser works on the canonical single-brace form.
    """

    return text.replace("{{%", "{%").replace("%}}", "%}")


def _strip_leading_blank_line(body: str) -> str:
    """Drop a single leading newline left over by the ``{% if %}`` tag.

    The template writes ``{% if mode == "semi" %}\n### SEMI...``, so the
    body captured by our regex starts with ``\n###...``.  Leaving that
    in place produces an extra blank line between the Workflow heading
    and the arm heading; stripping one leading newline (not all of
    them) keeps the rendered output tidy without collapsing intentional
    blank lines deeper in the body.
    """

    return body[1:] if body.startswith("\n") else body


def _strip_trailing_blank_line(body: str) -> str:
    """Drop a single trailing newline before ``{% endif %}``."""

    return body[:-1] if body.endswith("\n") else body


def _apply_conditionals(text: str, mode: str) -> str:
    """Expand / drop ``{% if mode == "semi|full" %}...{% endif %}`` blocks.

    Each block whose ``mode`` matches the argument is replaced by its
    body; each non-matching block is dropped entirely.  Unknown modes
    are treated as "drop everything conditional" — safer than raising,
    because the active-mode set is validated by the caller.
    """

    def _sub(match: re.Match[str]) -> str:
        arm = match.group("arm")
        body = match.group("body")
        if arm != mode:
            return ""
        body = _strip_leading_blank_line(body)
        body = _strip_trailing_blank_line(body)
        return body

    return _BLOCK_RE.sub(_sub, text)


# Only these names are substituted at render time.  Anything else in
# single braces (e.g. ``{number}`` inside the semi arm) stays as-is
# because it's meant for Claude to interpret per issue.
_RENDER_VARS = frozenset({
    "repo",
    "timestamp",
    "mode",
    "default_branch",
    "discovered_conventions",
    "issue_list",
})


def _substitute_variables(text: str, values: dict[str, str]) -> str:
    """Replace ``{name}`` with ``values[name]`` for names in ``values``.

    We do NOT use ``str.format`` here because the template contains
    other single-brace tokens that must survive verbatim.  Instead we
    run a literal ``str.replace`` per known variable, which is both
    fast (the template is ~3 KB) and predictable.
    """

    for name in _RENDER_VARS:
        placeholder = "{" + name + "}"
        text = text.replace(placeholder, values.get(name, ""))
    return text


def render_protocol(
    repo: str,
    mode: str,
    default_branch: str,
    discovered_conventions: str,
    queue_summary: str,
    timestamp: str,
) -> str:
    """Load and render the agent protocol template.

    Args:
        repo: ``owner/name`` string shown in the banner.
        mode: ``"semi"`` or ``"full"``.  Selects which workflow arm is
            included.  Unknown modes are treated as "neither arm" —
            the rules section still renders.
        default_branch: Detected default branch for the repo; the
            template only shows it in the banner, the per-issue branch
            rules defer to tool calls at runtime.
        discovered_conventions: Pre-rendered convention summary (from
            :mod:`ghia.convention_scan`).  May be empty.
        queue_summary: Pre-rendered queue markdown (see
            :func:`format_queue_summary`).  May be empty.
        timestamp: Human-readable session-start timestamp.

    Returns:
        The fully rendered protocol string, ready to be handed to
        Claude's context.

    Raises:
        ProtocolTemplateError: if the template file cannot be read.
    """

    path = template_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ProtocolTemplateError(
            f"agent protocol template not found at {path}"
        ) from exc
    except OSError as exc:
        raise ProtocolTemplateError(
            f"could not read agent protocol template at {path}: {exc}"
        ) from exc

    # Normalize the escaped block syntax before parsing.
    canonical = _unescape_jinja_braces(raw)

    # Apply mode-based conditional blocks first so their bodies also
    # get variable substitution.
    expanded = _apply_conditionals(canonical, mode)

    values = {
        "repo": repo,
        "timestamp": timestamp,
        "mode": mode,
        "default_branch": default_branch,
        "discovered_conventions": discovered_conventions or "(none detected)",
        "issue_list": queue_summary or (
            "(queue empty — the agent auto-populates from open issues "
            "matching the configured label(s) on every poll. Add the "
            "label to issues you want handled, then wait for the next "
            "tick or call issue_agent_fetch_now to refresh immediately.)"
        ),
    }
    return _substitute_variables(expanded, values)


def format_queue_summary(queue: Iterable[int]) -> str:
    """Render a short bullet list for the active queue.

    The renderer only has issue numbers at this stage — titles are
    fetched lazily by ``get_issue`` when the agent picks each one up,
    so every bullet ends with ``(title unknown until fetched)``.  An
    empty queue returns the empty string so ``render_protocol`` can
    swap in its own placeholder copy.
    """

    numbers = list(queue)
    if not numbers:
        return ""
    lines = [f"- #{n}: (title unknown until fetched)" for n in numbers]
    return "\n".join(lines)
