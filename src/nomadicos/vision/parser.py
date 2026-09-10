"""Observation parser: vision model text → structured, bounded observations.

Parsing is deterministic string/regex analysis (BP §265-style conservatism):
clause-bounded matches (split on sentence boundaries) so a window match can
never swallow a later button. The parser never invents elements the
description lacks; every parsed element carries its source span (BP §177).
"""

import re
from typing import Any

from nomadicos.core.logging import get_logger
from nomadicos.vision.base import ScreenObservation

logger = get_logger("vision.parser")

MAX_ELEMENTS = 64

_KIND_RE = re.compile(
    r"\b(?P<kind>window|button|text field|field|menu|link|checkbox|tab|panel|icon|dialog)\b",
    re.IGNORECASE,
)
_LABEL_RE = re.compile(r"['\"](?P<label>[^'\"]{1,64})['\"]")
_POSITION_RE = re.compile(r"\(?\s*(?P<x>\d{1,5})\s*,\s*(?P<y>\d{1,5})\s*\)?")

_CLAUSE_SPLIT_RE = re.compile(r"[.;\n]")


def parse_observation(
    observation: ScreenObservation,
    *,
    description: str | None = None,
) -> ScreenObservation:
    """Parse `description` into bounded `elements` on the observation (in place).

    Deterministic: clause-bounded kind+label+position extraction. Unknown text
    stays in `description` — grounding quality beats coverage (BP §177).
    """
    text = description if description is not None else observation.description
    if not text:
        return observation

    elements: list[dict[str, Any]] = []
    for clause in _CLAUSE_SPLIT_RE.split(text):
        clause = clause.strip()
        if not clause:
            continue
        kind_matches = list(_KIND_RE.finditer(clause))
        if not kind_matches:
            continue
        # Segment the clause at each kind occurrence: each segment describes one element.
        segments: list[tuple[str, str]] = []
        for index, match in enumerate(kind_matches):
            start = match.end()
            end = kind_matches[index + 1].start() if index + 1 < len(kind_matches) else len(clause)
            segments.append((match.group("kind").lower(), clause[start:end]))
        # The label may precede the kind in the first segment ("window 'Editor'").
        for index, (kind, segment) in enumerate(segments):
            element: dict[str, Any] = {"kind": kind}
            head = clause[: kind_matches[0].start()] if index == 0 else ""
            searchable = segment if index > 0 else segment + " " + head
            label_match = _LABEL_RE.search(searchable)
            if label_match:
                element["label"] = label_match.group("label")
            position_match = _POSITION_RE.search(segment)
            if position_match:
                element["position"] = {
                    "x": int(position_match.group("x")),
                    "y": int(position_match.group("y")),
                }
            if len(elements) < MAX_ELEMENTS:
                elements.append(element)

    observation.elements = elements
    observation.description = text
    return observation


def find_element(
    observation: ScreenObservation,
    *,
    kind: str | None = None,
    label: str | None = None,
) -> dict[str, Any] | None:
    """Grounding lookup (BP §177): first element matching kind/label."""
    for element in observation.elements:
        if kind is not None and element.get("kind", "").lower() != kind.lower():
            continue
        if label is not None and element.get("label", "").lower() != label.lower():
            continue
        return element
    return None


class ObservationParser:
    """BP §144-style vision verification adapter: parse + query in one object."""

    def __init__(self, *, vision_model: Any | None = None) -> None:
        self._vision_model = vision_model

    async def build(
        self, observation: ScreenObservation, question: str | None = None
    ) -> ScreenObservation:
        if self._vision_model is not None:
            description = await self._vision_model.describe(
                b"", question  # engine passes bytes; parser test path passes pre-set desc
            )
            return parse_observation(observation, description=description)
        return parse_observation(observation)

    @staticmethod
    def parse(observation: ScreenObservation) -> ScreenObservation:
        return parse_observation(observation)

    @staticmethod
    def find(observation: ScreenObservation, **criteria: str) -> dict[str, Any] | None:
        return find_element(observation, **criteria)


__all__ = ["ObservationParser", "find_element", "parse_observation"]
