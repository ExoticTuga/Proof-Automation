"""Recover plain proof-state text from Isabelle's HTML panel output.

With vscode_html_output=true, Pretty_Text_Panel emits HTML.source(html), i.e.
<pre class="source">...</pre> with nested <span> markup and <a> links. The
pretty-printer has already applied line breaking, so the *text content* of that
HTML is exactly the plain-text proof state -- no reflowing needed, just tag
stripping and entity unescaping.

Class names on the spans (keyword1, const, free, var, ...) are the same markup
that PIDE/decoration carries, so they are optionally preserved as spans.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass
class Span:
    start: int
    end: int
    kind: str


class _PanelParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.length = 0
        self.spans: list[Span] = []
        self._stack: list[tuple[str, int]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")
            self.length += 1
            return
        cls = dict(attrs).get("class")
        self._stack.append((cls, self.length))

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")
            self.length += 1

    def handle_endtag(self, tag):
        if tag == "br":
            return
        if not self._stack:
            return
        cls, start = self._stack.pop()
        if cls and self.length > start:
            self.spans.append(Span(start, self.length, cls))

    def handle_data(self, data):
        self.parts.append(data)
        self.length += len(data)

    @property
    def text(self) -> str:
        return "".join(self.parts)


def panel_html_to_text(content: str, with_spans: bool = False):
    """Return the plain text of an Isabelle panel payload.

    An empty panel arrives as '<pre class="source"/>' and yields ''.
    """
    if not content:
        return ("", []) if with_spans else ""
    p = _PanelParser()
    p.feed(content)
    p.close()
    text = p.text
    return (text, p.spans) if with_spans else text
