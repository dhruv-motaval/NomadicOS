"""Readable text from fetched pages, for observations (data, never authority).

A ``curl`` of a web page returns HTML whose first few hundred characters are
``<head>`` markup; cutting that to an observation budget leaves the model
nothing to read. Pages are turned into their visible text first, and an
unknown fact keeps the window of that text that best answers its question.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

#: elements whose content is never visible text
_SKIP = frozenset({"script", "style", "noscript", "svg", "template", "iframe"})
#: elements that start a new line in the readable text
_BLOCK = frozenset(
    {
        "p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "header", "footer", "table", "ul", "ol", "title", "pre",
    }
)  # fmt: skip
_HTML_SNIFF = re.compile(r"<!doctype html|<html[\s>]|<body[\s>]|<head[\s>]", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9$.]{3,}")
_STOP = frozenset({"what", "the", "and", "for", "with", "its", "this", "that", "from", "does"})


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def is_html(text: str) -> bool:
    return bool(_HTML_SNIFF.search(text[:2000]))


def readable_text(text: str) -> str:
    """Visible text of an HTML page (one line per block); other text as is."""
    if not is_html(text):
        return text
    parser = _TextParser()
    parser.feed(text)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def best_window(text: str, question: str, size: int) -> str:
    """The ``size``-character slice of ``text`` sharing the most words with
    ``question`` (the start of the text when nothing matches)."""
    if len(text) <= size:
        return text
    terms = set(_WORD.findall(question.lower())) - _STOP
    lowered = text.lower()
    best_start, best_score = 0, 0
    starts = [*range(0, len(text) - size + 1, max(1, size // 4)), len(text) - size]
    for start in starts:
        window = lowered[start : start + size]
        score = sum(window.count(term) for term in terms)
        if score > best_score:
            best_start, best_score = start, score
    return text[best_start : best_start + size]
