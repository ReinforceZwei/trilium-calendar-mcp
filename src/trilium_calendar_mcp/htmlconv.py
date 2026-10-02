"""Plain text <-> Trilium note HTML.

Trilium stores rich-text notes as HTML, but agents deal in plain text, so
descriptions are converted on the way in and on the way out. The converter is
deliberately small: it round-trips exactly what this server writes (one ``<p>``
per line, a blank line as ``<p><br></p>``) and degrades readably for HTML
edited by hand in the Trilium UI.

The one rule that matters: block boundaries are *collapsed* (``</p><p>`` is a
single line break) while ``<br>`` is an explicit break that always adds one.
Without that distinction, every paragraph boundary would read back as a blank
line and descriptions would grow a blank line per line on each round-trip.
"""

from __future__ import annotations

import html as _html
from html.parser import HTMLParser

_BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "blockquote",
    "pre",
    "ul",
    "ol",
    "table",
    "tr",
    "hr",
    "li",
}


def text_to_html(text: str) -> str:
    """Plain text -> Trilium HTML. One paragraph per line, blank line -> empty paragraph."""
    if not text:
        return "<p></p>"
    parts = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if not stripped:
            parts.append("<p><br></p>")
        else:
            leading = len(line) - len(line.lstrip(" "))
            body = _html.escape(stripped)
            if leading:
                body = "&nbsp;" * leading + body
            parts.append(f"<p>{body}</p>")
    return "".join(parts)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._link_stack: list[str] = []
        self._bold = 0
        self._italic = 0
        self._in_pre = False

    def _block_break(self) -> None:
        """A block boundary: one line break, never two in a row."""
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrd = {k.lower(): (v or "") for k, v in attrs}
        if tag == "br":
            self.parts.append("\n")  # explicit break: always counts
        elif tag in _BLOCK_TAGS:
            self._block_break()
            if tag == "li":
                self.parts.append("- ")
            elif tag == "pre":
                self._in_pre = True
        elif tag in ("strong", "b"):
            self._bold += 1
            self.parts.append("**")
        elif tag in ("em", "i"):
            self._italic += 1
            self.parts.append("*")
        elif tag == "a":
            self._link_stack.append(attrd.get("href", ""))
            self.parts.append("[")
        elif tag == "img":
            alt = attrd.get("alt", "")
            src = attrd.get("src", "")
            self.parts.append(f"![{alt}]({src})" if src else alt)
        elif tag in ("td", "th"):
            self.parts.append("\t")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            if tag == "pre":
                self._in_pre = False
            self._block_break()
        elif tag in ("strong", "b") and self._bold:
            self._bold -= 1
            self.parts.append("**")
        elif tag in ("em", "i") and self._italic:
            self._italic -= 1
            self.parts.append("*")
        elif tag == "a":
            href = self._link_stack.pop() if self._link_stack else ""
            self.parts.append(f"]({href})" if href else "]")

    def handle_data(self, data: str) -> None:
        self.parts.append(data if self._in_pre else data.replace("\n", " "))


def html_to_text(html: str) -> str:
    """Trilium HTML -> plain text (markdown-ish links/emphasis preserved)."""
    if not html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed HTML: fall back to a crude strip
        return _html.unescape(html).strip()

    lines = [line.rstrip() for line in "".join(parser.parts).split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()
