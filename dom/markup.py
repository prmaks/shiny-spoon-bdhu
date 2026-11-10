"""A small, safe markdown-to-HTML renderer (escape first, then a limited syntax subset)."""

from __future__ import annotations

import html
import re

from markupsafe import Markup

from .parsing import slugify, unique_anchor

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_ULIST = re.compile(r"^\s*[-*+]\s+(.*)$")
_OLIST = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\*)|(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)")
_SAFE_URL = re.compile(r"^(https?://|/|#|\.{0,2}/|[\w\-.]+(\.[a-z]{2,5})?$)", re.IGNORECASE)


def inline(text: str) -> str:
    """Render inline markup. Input is raw text; output is safe HTML."""
    codes: list[str] = []

    def stash(m: re.Match) -> str:
        codes.append(f"<code>{html.escape(m.group(1))}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = _CODE.sub(stash, text)
    text = html.escape(text, quote=True)

    def link(m: re.Match) -> str:
        label, url = m.group(1), html.unescape(m.group(2))
        if not _SAFE_URL.match(url):
            return m.group(0)
        return f'<a href="{html.escape(url, quote=True)}">{label}</a>'

    text = _LINK.sub(link, text)
    text = _BOLD.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", text)
    text = _ITALIC.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", text)
    text = re.sub(r"(?: {2,}|\\)\n", "<br>\n", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def render(text: str, used_anchors: set[str] | None = None, heading_shift: int = 0) -> Markup:
    """Render a markdown fragment. Headings get unique anchors from ``used_anchors``."""
    used = used_anchors if used_anchors is not None else set()
    out: list[str] = []
    para: list[str] = []
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0

    def flush() -> None:
        if para:
            out.append(f"<p>{inline(chr(10).join(para))}</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            block = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(block))}</code></pre>")
            i += 1
            continue
        if not stripped:
            flush()
            i += 1
            continue
        m = _HEADING.match(line)
        if m:
            flush()
            level = min(6, len(m.group(1)) + heading_shift)
            anchor = unique_anchor(slugify(m.group(2)), used)
            out.append(
                f'<h{level} id="{html.escape(anchor)}">{inline(m.group(2))}'
                f' <a class="anchor" href="#{html.escape(anchor)}">#</a></h{level}>'
            )
            i += 1
            continue
        if _HR.match(line):
            flush()
            out.append("<hr>")
            i += 1
            continue
        if stripped.startswith(">"):
            flush()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append(f"<blockquote>{render(chr(10).join(quote), used)}</blockquote>")
            continue
        for regex, tag in ((_ULIST, "ul"), (_OLIST, "ol")):
            if regex.match(line):
                flush()
                items = []
                while i < len(lines) and regex.match(lines[i]):
                    items.append(f"<li>{inline(regex.match(lines[i]).group(1))}</li>")
                    i += 1
                out.append(f"<{tag}>{''.join(items)}</{tag}>")
                break
        else:
            para.append(line)
            i += 1
    flush()
    return Markup("\n".join(out))


def excerpt(text: str, limit: int = 280) -> str:
    """Plain-text excerpt of markdown."""
    plain = re.sub(r"[#>*_`\[\]]", "", text)
    plain = re.sub(r"\(\S+?\)", "", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain if len(plain) <= limit else plain[: limit - 1].rsplit(" ", 1)[0] + "…"
