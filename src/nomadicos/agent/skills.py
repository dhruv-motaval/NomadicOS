"""Machine-local learned skills (caveman/ponytail style).

One markdown file per task type under ``data/skills/``: telegraphic facts the
agent learned from previous runs on THIS machine (exact commands, paths,
quirks). Everything stays local — storage here, generation via the local model
(I11: no data ever leaves the PC).

Skill files are deliberately token-minimal: only injected when a new goal
matches by word overlap, capped to a few lines.
"""
from __future__ import annotations

import re
from pathlib import Path

_STOPWORDS = frozenset(
    "a an and the to into for of on in with my this that it please can you me some".split()
)
_MAX_LINES = 10
_MAX_MATCHES = 2


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _slug(goal: str) -> str:
    words = [w for w in _tokens(goal)][:5]
    return "-".join(words) or "task"


class SkillStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def save(self, goal: str, content: str) -> Path:
        """Persist a skill note, capped to a few telegraphic lines."""
        lines = [
            line.strip()
            for line in content.strip().splitlines()
            if line.strip()
        ][:_MAX_LINES]
        path = self._root / f"{_slug(goal)}.md"
        text = "\n".join(lines)
        path.write_text(text + ("\n" if text else ""), encoding="utf-8")
        return path

    def find(self, goal: str, *, min_overlap: int = 2) -> list[str]:
        """Return matching skill notes, best word-overlap first."""
        goal_tokens = _tokens(goal)
        if not goal_tokens:
            return []
        scored: list[tuple[int, str]] = []
        for path in self._root.glob("*.md"):
            content = path.read_text(encoding="utf-8", errors="replace")
            # Slug words (the original goal) count strongly — they are the
            # retrieval key; body content adds recall.
            file_tokens = _tokens(path.stem) | _tokens(content)
            overlap = len(goal_tokens & file_tokens)
            if overlap >= min_overlap:
                scored.append((overlap, content.strip()))
        scored.sort(key=lambda pair: -pair[0])
        return [content for _, content in scored[:_MAX_MATCHES]]


__all__ = ["SkillStore"]
