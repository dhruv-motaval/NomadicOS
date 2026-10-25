"""README/Markdown structural validation (post-release hardening).

The repository has no Markdown linter; these tests pin the invariants that
a malformed edit broke once already (PR #2 review finding 1):

- fenced code blocks in the README are balanced (every opening fence has
  exactly one closing fence), so all later headings and content render as
  normal Markdown instead of landing inside an accidental code block;
- a Mermaid diagram body is not duplicated after its closing fence (the
  ``class blocked,partial outcome`` duplication bug).
"""

from __future__ import annotations

from pathlib import Path

README = Path(__file__).resolve().parents[2] / "README.md"


def _fence_state(lines: list[str]) -> int:
    """1 while inside a fenced block, 0 outside."""
    state = 0
    for raw in lines:
        if raw.strip().startswith("```"):
            state = 1 - state
    return state


def test_readme_exists() -> None:
    assert README.exists()


def test_readme_code_fences_are_balanced() -> None:
    """Every fence toggles the block state; the document must end closed."""
    assert _fence_state(README.read_text(encoding="utf-8").splitlines()) == 0, (
        "unbalanced code fences: README ends inside a code block"
    )


def test_readme_mermaid_body_not_duplicated_after_close() -> None:
    """Regression: 'class blocked,partial outcome' must not appear as stray
    content after the architecture diagram's closing fence."""
    joined = README.read_text(encoding="utf-8")
    marker = "class blocked,partial outcome\n```\n\n    class blocked,partial outcome"
    assert marker not in joined


def test_readme_headings_render_outside_code_blocks() -> None:
    """All headings land outside fenced code blocks."""
    lines = README.read_text(encoding="utf-8").splitlines()
    inside = False
    headings: list[str] = []
    for raw in lines:
        if raw.strip().startswith("```"):
            inside = not inside
            continue
        if not inside and raw.startswith("## "):
            headings.append(raw.strip())
    for expected in (
        "## The idea",
        "## What is implemented",
        "## Security and authority model",
        "## Architecture",
        "## Local inference",
        "## Local PostgreSQL (development)",
        "## Install",
        "## CLI quickstart",
        "## Testing and quality checks",
        "## Completion evidence",
        "## Project status and direction",
        "## License",
    ):
        assert expected in headings, f"heading swallowed by a code fence: {expected}"
