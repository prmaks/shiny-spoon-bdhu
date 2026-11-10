"""Tolerant parsers for the hand-written markdown files and embedded image metadata."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date

DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
# "## 2025-03-14 — Title" (em/en dash, hyphen or colon as separator, title optional)
DATED_HEADING_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*(?:[—–:-]+\s*)?(.*)$")
H2_RE = re.compile(r"^##\s+(.*?)\s*#*\s*$")
H1_RE = re.compile(r"^#\s+(.*?)\s*#*\s*$")
DIMS_RE = re.compile(r"(\d{2,6})\s*[x×х]\s*(\d{2,6})", re.IGNORECASE)
SIZE_RE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:[KMGkmg]i?B|[КМГкмг]Б|байт|B)\b", re.IGNORECASE)
SEED_RE = re.compile(r"(?:seed|сид)\s*[:=#]?\s*(-?\d+)", re.IGNORECASE)
BACKTICK_RE = re.compile(r"`([^`]+)`")


def valid_date(text: str | None) -> str | None:
    """Return YYYY-MM-DD if text contains a plausible date."""
    if not text:
        return None
    m = DATE_RE.search(text)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    if 1900 <= y <= 2200 and 1 <= mo <= 12 and 1 <= d <= 31:
        return m.group(0)
    return None


RU_MONTHS = {
    name: i
    for i, name in enumerate(
        (
            "января", "февраля", "марта", "апреля", "мая", "июня",
            "июля", "августа", "сентября", "октября", "ноября", "декабря",
        ),
        start=1,
    )
}  # fmt: skip
RU_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
# "16 октября 2026", optional "г." and ", вторник"; the rest is the title.
RU_DATED_HEADING_RE = re.compile(
    rf"^\s*(\d{{1,2}})\s+({'|'.join(RU_MONTHS)})\s+(\d{{4}})(?!\d)"
    r"(?:\s*г(?:\.|(?!\w)))?"
    rf"(?:\s*,\s*(?:{'|'.join(RU_WEEKDAYS)})(?!\w))?"
    r"(.*)$",
    re.IGNORECASE,
)
# "31.10.2026", optional "г." and ", 05:10"; the rest is the title.
DOTTED_HEADING_RE = re.compile(
    r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)"
    r"(?:\s*г(?:\.|(?!\w)))?"
    r"(?:\s*,?\s*\d{1,2}:\d{2}(?!\d))?"
    r"(.*)$"
)
TITLE_LEAD_RE = re.compile(r"^(?:[\s:,·—–-]|г\.)+")


def _clean_title(rest: str) -> str:
    return TITLE_LEAD_RE.sub("", rest).strip()


def _iso(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_dated_heading(heading: str) -> tuple[str, str] | None:
    """Parse an ISO, Russian ('16 октября 2026') or dotted ('31.10.2026') dated heading
    into (ISO day, title); None if the heading does not start with a valid date."""
    m = DATED_HEADING_RE.match(heading)
    if m and valid_date(m.group(1)):
        return m.group(1), _clean_title(m.group(2))
    m = RU_DATED_HEADING_RE.match(heading)
    if m:
        iso = _iso(int(m.group(3)), RU_MONTHS[m.group(2).lower()], int(m.group(1)))
        return (iso, _clean_title(m.group(4))) if iso else None
    m = DOTTED_HEADING_RE.match(heading)
    if m:
        iso = _iso(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        return (iso, _clean_title(m.group(4))) if iso else None
    return None


_SLUG_STRIP = re.compile(r"[^\w\s-]", re.UNICODE)


def slugify(text: str) -> str:
    text = _SLUG_STRIP.sub("", text.lower()).strip()
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-") or "section"


def unique_anchor(base: str, used: set[str]) -> str:
    anchor, n = base, 2
    while anchor in used:
        anchor = f"{base}-{n}"
        n += 1
    used.add(anchor)
    return anchor


# --- generic sections ---------------------------------------------------------


@dataclass
class Section:
    heading: str
    body: str
    day: str | None = None
    title: str = ""
    anchor: str = ""


@dataclass
class ParsedDoc:
    title: str | None
    preamble: str
    sections: list[Section] = field(default_factory=list)


def split_sections(text: str) -> ParsedDoc:
    """Split markdown into an optional H1 title, preamble and level-2 sections."""
    title: str | None = None
    preamble: list[str] = []
    raw: list[tuple[str, list[str]]] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        h2 = None if in_fence else H2_RE.match(line)
        if h2:
            raw.append((h2.group(1), []))
            continue
        if not in_fence and title is None and not raw:
            h1 = H1_RE.match(line)
            if h1:
                title = h1.group(1)
                continue
        (raw[-1][1] if raw else preamble).append(line)
    sections = [Section(heading=h, body="\n".join(b).strip("\n")) for h, b in raw]
    doc = ParsedDoc(title=title, preamble="\n".join(preamble).strip("\n"), sections=sections)
    used: set[str] = set()
    for s in doc.sections:
        dated = parse_dated_heading(s.heading)
        if dated:
            s.day, s.title = dated
            s.anchor = unique_anchor(s.day, used)
        else:
            s.title = s.heading
            s.anchor = unique_anchor(slugify(s.heading), used)
    return doc


# --- catalogue ------------------------------------------------------------------


@dataclass
class CatalogueEntry:
    position: int
    day: str | None
    title: str
    filenames: list[str]
    format: str | None = None
    width: int | None = None
    height: int | None = None
    size_text: str | None = None
    model: str | None = None
    seed: str | None = None
    prose: str = ""


def _parse_meta_line(line: str, entry: CatalogueEntry) -> None:
    entry.filenames = [n.strip() for n in BACKTICK_RE.findall(line) if n.strip()]
    rest = BACKTICK_RE.sub("", line)
    parts = [p.strip() for p in re.split(r"\s*[·•|]\s*", rest) if p.strip()]
    leftovers: list[str] = []
    for part in parts:
        dims = DIMS_RE.search(part)
        if dims and entry.width is None:
            entry.width, entry.height = int(dims.group(1)), int(dims.group(2))
            fmt = part[: dims.start()].strip(" ,")
            entry.format = fmt.upper() or None
            continue
        if entry.size_text is None and SIZE_RE.fullmatch(part):
            entry.size_text = part
            continue
        seed = SEED_RE.search(part)
        if seed:
            entry.seed = seed.group(1)
            model = SEED_RE.sub("", part).strip(" ,;")
            if model:
                entry.model = model
            continue
        leftovers.append(part)
    if entry.model is None and leftovers:
        entry.model = leftovers[-1].strip(" ,;") or None


def parse_catalogue(text: str) -> list[CatalogueEntry]:
    doc = split_sections(text)
    entries: list[CatalogueEntry] = []
    for i, s in enumerate(doc.sections):
        entry = CatalogueEntry(position=i, day=s.day, title=s.title or s.heading, filenames=[])
        lines = s.body.splitlines()
        prose_start = 0
        for j, line in enumerate(lines):
            if not line.strip():
                continue
            if BACKTICK_RE.search(line):
                _parse_meta_line(line, entry)
                prose_start = j + 1
            break
        entry.prose = "\n".join(lines[prose_start:]).strip()
        entries.append(entry)
    return entries


# --- checksums -------------------------------------------------------------------

CHECKSUM_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")


def parse_checksums(text: str) -> tuple[dict[str, str], list[int]]:
    """Return {path: sha256} and the list of unparseable line numbers."""
    result: dict[str, str] = {}
    bad: list[int] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = CHECKSUM_RE.match(line)
        if m:
            result[m.group(2).strip()] = m.group(1).lower()
        else:
            bad.append(n)
    return result, bad


# --- embedded image metadata -------------------------------------------------------


@dataclass
class EmbeddedMeta:
    prompt: str | None = None
    seed: str | None = None
    model: str | None = None


_A1111_KV = re.compile(r"(?:^|,\s*)([A-Za-z][\w ]*?):\s*([^,]+)")


def _parse_a1111(text: str, meta: EmbeddedMeta) -> None:
    lines = text.strip().splitlines()
    if not lines:
        return
    settings_idx = next(
        (i for i in range(len(lines) - 1, -1, -1) if "Steps:" in lines[i] or "Seed:" in lines[i]),
        None,
    )
    body = lines if settings_idx is None else lines[:settings_idx]
    prompt_lines = []
    for line in body:
        if line.startswith("Negative prompt:"):
            break
        prompt_lines.append(line)
    if prompt_lines:
        meta.prompt = meta.prompt or "\n".join(prompt_lines).strip() or None
    if settings_idx is not None:
        kv = {k.strip().lower(): v.strip() for k, v in _A1111_KV.findall(lines[settings_idx])}
        meta.seed = meta.seed or kv.get("seed")
        meta.model = meta.model or kv.get("model") or kv.get("model hash")


def _walk_comfy(data: object, meta: EmbeddedMeta) -> None:
    if not isinstance(data, dict):
        return
    nodes = data.get("nodes") if isinstance(data.get("nodes"), list) else data.values()
    texts: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        for key in ("seed", "noise_seed"):
            if meta.seed is None and isinstance(inputs.get(key), int | str):
                meta.seed = str(inputs[key])
        for key in ("ckpt_name", "unet_name", "model_name"):
            if meta.model is None and isinstance(inputs.get(key), str):
                meta.model = inputs[key]
        if "CLIPTextEncode" in str(node.get("class_type", "")) and isinstance(
            inputs.get("text"), str
        ):
            texts.append(inputs["text"])
    if texts and meta.prompt is None:
        meta.prompt = texts[0]


def extract_embedded(info: dict, exif: dict[str, str] | None = None) -> EmbeddedMeta:
    """Pull prompt/seed/model out of PNG text chunks or EXIF strings. Never raises."""
    meta = EmbeddedMeta()
    lower = {str(k).lower(): v for k, v in info.items() if isinstance(v, str | bytes)}
    for key, value in list(lower.items()):
        if isinstance(value, bytes):
            lower[key] = value.decode("utf-8", "replace")
    for key in ("seed",):
        if key in lower:
            meta.seed = lower[key].strip()
    for key in ("model", "model_name", "ckpt"):
        if key in lower and not meta.model:
            meta.model = lower[key].strip()
    if "parameters" in lower:
        _parse_a1111(lower["parameters"], meta)
    for key in ("prompt", "workflow"):
        raw = lower.get(key)
        if not raw:
            continue
        try:
            _walk_comfy(json.loads(raw), meta)
        except (ValueError, TypeError):
            if key == "prompt" and meta.prompt is None:
                meta.prompt = raw.strip()
    for key in ("description", "comment", "title"):
        if meta.prompt is None and lower.get(key):
            meta.prompt = lower[key].strip()
    if exif:
        for key in ("UserComment", "ImageDescription", "XPComment"):
            value = (exif.get(key) or "").strip()
            if not value:
                continue
            if "Steps:" in value or "Seed:" in value:
                _parse_a1111(value, meta)
            elif meta.prompt is None:
                meta.prompt = value
        if meta.model is None and exif.get("Software"):
            meta.model = exif["Software"].strip()
    if meta.seed is not None:
        meta.seed = str(meta.seed)[:64]
    return meta
