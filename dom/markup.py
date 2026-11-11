"""A small, safe markdown-to-HTML renderer (escape first, then a limited syntax subset).

Besides basic markdown it turns references to sibling media files into embeds:
backticked or «guillemet-quoted» names ending in an image/audio/html extension,
markdown images, links to audio and lines that are just an image path.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from pathlib import PurePosixPath
from urllib.parse import quote

from markupsafe import Markup

from .parsing import slugify, unique_anchor

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_ULIST = re.compile(r"^\s*[-*+]\s+(.*)$")
_OLIST = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_IMAGE = re.compile(r"(\s*)!\[([^\]]*)\]\(([^)\s]+)\)")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\*)|(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)")
_SAFE_URL = re.compile(r"^(https?://|/|#|\.{0,2}/|[\w\-.]+(\.[a-z]{2,5})?$)", re.IGNORECASE)
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")

IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}
AUDIO_EXTS = {"ogg", "mp3", "wav"}
PAGE_EXTS = {"html", "htm"}
_EXT = "|".join(sorted(IMAGE_EXTS | AUDIO_EXTS | PAGE_EXTS))
_AUDIO = "|".join(sorted(AUDIO_EXTS))
_IMG = "|".join(sorted(IMAGE_EXTS))
_MEDIA_TOKEN = re.compile(rf"^[^\n`«»<>\"]{{1,200}}\.(?:{_EXT})$", re.IGNORECASE)
_GUILLEMET = re.compile(rf"(\s*)«([^«»\n<>]{{1,200}}?\.(?:{_EXT}))»", re.IGNORECASE)
_AUDIO_LINK = re.compile(rf"\[([^\]]+)\]\(([^)\s]+\.(?:{_AUDIO}))\)", re.IGNORECASE)
_BARE_IMAGE = re.compile(rf"^[^\s`«»<>\"]+\.(?:{_IMG})$", re.IGNORECASE)
# Every media reference form; used to tell a "just the embed" paragraph from prose.
_MEDIA_ANY = re.compile(
    rf"`[^`\n]+\.(?:{_EXT})`|«[^«»\n]+\.(?:{_EXT})»|!\[[^\]]*\]\([^)\s]+\)"
    rf"|\[[^\]]+\]\([^)\s]+\.(?:{_AUDIO})\)",
    re.IGNORECASE,
)

Resolver = Callable[[str], str | None]
"""Maps a reference as written to a data-dir relative path, or None if it is missing."""


def safe_relpath(token: str) -> str | None:
    token = token.strip().removeprefix("./")
    if not token or "://" in token or token.startswith(("/", "\\")) or ":" in token[:3]:
        return None
    if any(part.startswith(".") for part in PurePosixPath(token).parts):
        return None
    return token


def _default_resolve(token: str) -> str | None:
    return safe_relpath(token)


def media_html(token: str, resolve: Resolver | None, alt: str = "") -> str | None:
    """Embed for a media reference; None if ``token`` is not one (render it as usual)."""
    if not _MEDIA_TOKEN.match(token.strip()) or safe_relpath(token) is None:
        return None
    ext = token.rsplit(".", 1)[1].lower()
    name = html.escape(PurePosixPath(token.strip()).name)
    relpath = (resolve or _default_resolve)(token)
    if relpath is None:
        return (
            f'<figure class="media missing"><div class="placeholder">файл не найден</div>'
            f"<figcaption>{name}</figcaption></figure>"
        )
    q = html.escape(quote(relpath))
    if ext in PAGE_EXTS:
        return (
            f'<figure class="embed"><iframe loading="lazy" title="{name}" '
            f'sandbox="allow-scripts allow-same-origin" src="/page/{q}"></iframe>'
            f'<figcaption>интерактивная страница · <a href="/page/{q}" target="_blank" '
            f'rel="noopener">открыть отдельно</a></figcaption></figure>'
        )
    if ext in AUDIO_EXTS:
        return (
            f'<figure class="media audio"><audio controls preload="metadata" '
            f'src="/media/file/{q}"></audio><figcaption>{name}</figcaption></figure>'
        )
    caption = f"<figcaption>{html.escape(alt)}</figcaption>" if alt else ""
    return (
        f'<figure class="media"><img loading="lazy" src="/media/file/{q}" '
        f'alt="{html.escape(alt or name)}">{caption}</figure>'
    )


def inline(
    text: str,
    blocks: list[str] | None = None,
    resolve: Resolver | None = None,
    quiet: bool = False,
) -> str:
    """Render inline markup. Input is raw text; output is safe HTML.

    With ``blocks``, media references are rendered as embeds appended to ``blocks``
    (they are not phrasing content, so the caller places them after the paragraph);
    the reference itself stays in the text as a short mention unless ``quiet``.
    """
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    def media(token: str, mention: str, alt: str = "") -> str | None:
        if blocks is None:
            return None
        embed = media_html(token, resolve, alt)
        if embed is None:
            return None
        blocks.append(embed)
        # Images are fully replaced by their figure; pages and sounds keep a mention.
        is_image = token.rsplit(".", 1)[-1].lower() in IMAGE_EXTS
        return keep("" if quiet or is_image else mention)

    def code(m: re.Match) -> str:
        token = m.group(1)
        mention = f"<code>{html.escape(token)}</code>"
        return media(token, mention) or keep(mention)

    text = _CODE.sub(code, text)

    def ref(m: re.Match, token: str, mention: str, alt: str = "") -> str:
        placeholder = media(token, mention, alt)
        if placeholder is None:
            return m.group(0)
        # A removed image takes the space before it along ("табличка «x.png»." -> "табличка.").
        is_image = token.rsplit(".", 1)[-1].lower() in IMAGE_EXTS
        return placeholder if is_image else m.group(1) + placeholder

    text = _IMAGE.sub(lambda m: ref(m, m.group(3), "", alt=m.group(2)), text)
    text = _AUDIO_LINK.sub(
        lambda m: media(m.group(2), html.escape(m.group(1))) or m.group(0),
        text,
    )
    text = _GUILLEMET.sub(
        lambda m: ref(m, m.group(2), html.escape(f"«{m.group(2)}»")),
        text,
    )
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
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


def _cells(line: str) -> list[str]:
    line = line.strip()
    line = line[1:] if line.startswith("|") else line
    line = line[:-1] if line.endswith("|") else line
    return [c.strip() for c in line.split("|")]


def _table(head: str, rows: list[str]) -> str:
    th = "".join(f"<th>{inline(c)}</th>" for c in _cells(head))
    body = "".join(
        "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in _cells(r)) + "</tr>" for r in rows
    )
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def render(
    text: str,
    used_anchors: set[str] | None = None,
    heading_shift: int = 0,
    resolve: Resolver | None = None,
) -> Markup:
    """Render a markdown fragment. Headings get unique anchors from ``used_anchors``."""
    used = used_anchors if used_anchors is not None else set()
    out: list[str] = []
    para: list[str] = []
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0

    def flush() -> None:
        if not para:
            return
        raw = chr(10).join(para)
        para.clear()
        blocks: list[str] = []
        only_media = not _MEDIA_ANY.sub("", raw).strip(" \t\n.,:;—–-")
        body = inline(raw, blocks, resolve, quiet=only_media)
        if not (only_media and blocks):
            out.append(f"<p>{body}</p>")
        out.extend(blocks)

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
        if stripped.startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush()
            head, rows = line, []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(_table(head, rows))
            continue
        if _BARE_IMAGE.match(stripped) and (embed := media_html(stripped, resolve)):
            flush()
            out.append(embed)
            i += 1
            continue
        if stripped.startswith(">"):
            flush()
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            inner = render(chr(10).join(quote_lines), used, resolve=resolve)
            out.append(f"<blockquote>{inner}</blockquote>")
            continue
        for regex, tag in ((_ULIST, "ul"), (_OLIST, "ol")):
            if regex.match(line):
                flush()
                items = []
                while i < len(lines) and regex.match(lines[i]):
                    blocks: list[str] = []
                    body = inline(regex.match(lines[i]).group(1), blocks, resolve)
                    items.append(f"<li>{body}{''.join(blocks)}</li>")
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
