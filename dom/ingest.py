"""Ingest: walk the (read-only) data dir and upsert everything into SQLite."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import threading
import time
import unicodedata
import wave
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from PIL import ExifTags, Image, ImageOps

from .config import Config
from .parsing import (
    extract_embedded,
    parse_catalogue,
    parse_checksums,
    slugify,
    split_sections,
    unique_anchor,
    valid_date,
)

ART_ROOTS = ("arts", "арты")
AUDIO_ROOTS = ("голосовые", "звуки")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
AUDIO_EXTS = {".ogg", ".mp3", ".wav", ".flac"}
CATALOGUE = ("арты", "каталог.md")
CHECKSUMS = ("арты", "контрольные-суммы.txt")
DIARY = "дневник.md"
STORIES = "рассказы"
WARDROBE = "гардероб.md"
THUMB_MAX = 480

_lock = threading.Lock()


class IngestBusy(RuntimeError):
    pass


@dataclass
class IngestReport:
    started_at: str = ""
    duration_s: float = 0.0
    artworks: int = 0
    artworks_new: int = 0
    artworks_updated: int = 0
    artworks_removed: int = 0
    catalogue_entries: int = 0
    catalogue_linked: int = 0
    catalogue_unlinked: list[str] = field(default_factory=list)
    checksums_ok: int = 0
    checksum_mismatches: list[str] = field(default_factory=list)
    checksums_missing_files: list[str] = field(default_factory=list)
    diary_entries: int = 0
    stories: int = 0
    wardrobe: bool = False
    audio: int = 0
    thumbnails_created: int = 0
    missing: list[str] = field(default_factory=list)
    problems: list[dict[str, str]] = field(default_factory=list)

    def problem(self, path: str, error: str) -> None:
        self.problems.append({"path": path, "error": error})

    def to_dict(self) -> dict:
        return asdict(self)


# --- filesystem helpers (read-only) ----------------------------------------------------


def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)


def find_child(parent: Path, name: str) -> Path | None:
    """Find a child by name, tolerating NFC/NFD differences (e.g. files copied from macOS)."""
    direct = parent / name
    if direct.exists():
        return direct
    if not parent.is_dir():
        return None
    target = _nfc(name).casefold()
    try:
        for child in parent.iterdir():
            if _nfc(child.name).casefold() == target:
                return child
    except OSError:
        return None
    return None


def rel(path: Path, root: Path) -> str:
    return _nfc(path.relative_to(root).as_posix())


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(path: Path, report: IngestReport, data_dir: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        report.problem(rel(path, data_dir), f"не удалось прочитать: {exc}")
        return None
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        report.problem(rel(path, data_dir), "файл не в UTF-8, нечитаемые символы заменены")
        return raw.decode("utf-8", errors="replace")


def walk_files(root: Path, exts: set[str], report: IngestReport, data_dir: Path):
    def onerror(exc: OSError) -> None:
        report.problem(str(exc.filename or root), f"ошибка обхода каталога: {exc.strerror}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            if p.suffix.lower() in exts and p.is_file():
                yield p


def mtime_day(mtime: float) -> str:
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")


def derive_day(parts: list[str], mtime: float) -> str:
    for part in [*parts[:-1], parts[-1]]:
        day = valid_date(part)
        if day:
            return day
    return mtime_day(mtime)


def thumb_path(cache_dir: Path, sha: str) -> Path:
    return cache_dir / "thumbs" / sha[:2] / f"{sha}.webp"


# --- image handling -----------------------------------------------------------------


def _exif_strings(im: Image.Image) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        exif = im.getexif()
    except Exception:
        return out
    items = dict(exif.items())
    with contextlib.suppress(Exception):
        items.update(exif.get_ifd(ExifTags.IFD.Exif))
    for tag_id, value in items.items():
        name = ExifTags.TAGS.get(tag_id, str(tag_id))
        if name not in {"UserComment", "ImageDescription", "XPComment", "Software"}:
            continue
        if isinstance(value, bytes):
            if name == "XPComment":
                text = value.decode("utf-16-le", "ignore")
            elif name == "UserComment" and len(value) >= 8:
                prefix, body = value[:8], value[8:]
                if prefix.startswith(b"UNICODE"):
                    enc = "utf-16-be" if body[:1] == b"\x00" else "utf-16-le"
                    text = body.decode(enc, "ignore")
                else:
                    text = body.decode("utf-8", "ignore")
            else:
                text = value.decode("utf-8", "ignore")
        else:
            text = str(value)
        text = text.replace("\x00", "").strip()
        if text:
            out[name] = text
    return out


def make_thumbnail(im: Image.Image, dest: Path) -> None:
    if getattr(im, "is_animated", False):
        im.seek(0)
    frame = ImageOps.exif_transpose(im)
    has_alpha = frame.mode in ("RGBA", "LA", "PA") or (
        frame.mode == "P" and "transparency" in frame.info
    )
    frame = frame.convert("RGBA" if has_alpha else "RGB")
    frame.thumbnail((THUMB_MAX, THUMB_MAX), Image.Resampling.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    frame.save(tmp, "WEBP", quality=80, method=4)
    tmp.replace(dest)


# --- main entry point ---------------------------------------------------------------


def ingest(cfg: Config, conn: sqlite3.Connection, *, thumbnails: bool = True) -> IngestReport:
    if not _lock.acquire(blocking=False):
        raise IngestBusy("Индексация уже идёт")
    try:
        return _ingest(cfg, conn, thumbnails=thumbnails)
    finally:
        _lock.release()


def _ingest(cfg: Config, conn: sqlite3.Connection, *, thumbnails: bool) -> IngestReport:
    t0 = time.monotonic()
    report = IngestReport(started_at=datetime.now(UTC).isoformat(timespec="seconds"))
    data_dir = cfg.data_dir
    if not data_dir.is_dir():
        report.missing.append(str(data_dir))
    try:
        conn.execute("BEGIN")
        _ingest_artworks(cfg, conn, report, thumbnails)
        _ingest_checksums(conn, report, data_dir)
        _ingest_catalogue(conn, report, data_dir)
        _ingest_documents(conn, report, data_dir)
        _ingest_audio(conn, report, data_dir)
        _rebuild_search(conn)
        report.duration_s = round(time.monotonic() - t0, 3)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_ingest', ?)",
            (json.dumps(report.to_dict(), ensure_ascii=False),),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return report


def _ingest_artworks(
    cfg: Config, conn: sqlite3.Connection, report: IngestReport, thumbnails: bool
) -> None:
    data_dir = cfg.data_dir
    existing = {
        r["path"]: r
        for r in conn.execute("SELECT id, path, size_bytes, mtime, sha256, has_thumb FROM artworks")
    }
    seen: set[str] = set()
    for root_name in ART_ROOTS:
        root = find_child(data_dir, root_name)
        if root is None or not root.is_dir():
            report.missing.append(f"{root_name}/")
            continue
        for path in walk_files(root, IMAGE_EXTS, report, data_dir):
            relpath = rel(path, data_dir)
            if relpath in seen:
                continue
            seen.add(relpath)
            try:
                _ingest_one_artwork(cfg, conn, report, root, path, relpath, existing, thumbnails)
            except Exception as exc:  # one bad file must never abort the run
                report.problem(relpath, f"{type(exc).__name__}: {exc}")
    gone = [p for p in existing if p not in seen]
    for chunk_start in range(0, len(gone), 500):
        chunk = gone[chunk_start : chunk_start + 500]
        conn.execute(
            f"DELETE FROM artworks WHERE path IN ({','.join('?' * len(chunk))})", chunk
        )
    report.artworks_removed = len(gone)
    report.artworks = conn.execute("SELECT COUNT(*) FROM artworks").fetchone()[0]


def _ingest_one_artwork(cfg, conn, report, root, path, relpath, existing, thumbnails) -> None:
    st = path.stat()
    old = existing.get(relpath)
    parts = Path(rel(path, root)).parts
    folder = "/".join(parts[:-1])
    day = derive_day(list(parts), st.st_mtime)
    source = _nfc(root.name)
    unchanged = old is not None and old["size_bytes"] == st.st_size and old["mtime"] == st.st_mtime
    if unchanged:
        thumb = thumb_path(cfg.cache_dir, old["sha256"])
        has_thumb = thumb.exists()
        if thumbnails and not has_thumb:
            try:
                with Image.open(path) as im:
                    make_thumbnail(im, thumb)
                has_thumb = True
                report.thumbnails_created += 1
            except Exception as exc:
                report.problem(relpath, f"не удалось сделать миниатюру: {exc}")
        conn.execute(
            "UPDATE artworks SET day=?, folder=?, source=?, has_thumb=? WHERE id=?",
            (day, folder, source, int(has_thumb), old["id"]),
        )
        return

    sha = sha256_file(path)
    width = height = None
    meta = None
    has_thumb = False
    thumb = thumb_path(cfg.cache_dir, sha)
    try:
        with Image.open(path) as im:
            width, height = im.size
            meta = extract_embedded(dict(im.info), _exif_strings(im))
            if thumb.exists():
                has_thumb = True
            elif thumbnails:
                make_thumbnail(im, thumb)
                has_thumb = True
                report.thumbnails_created += 1
    except Exception as exc:
        report.problem(relpath, f"не удалось разобрать изображение: {type(exc).__name__}: {exc}")
    conn.execute(
        """
        INSERT INTO artworks(path, filename, source, folder, day, ext, size_bytes, mtime, sha256,
                             width, height, prompt, seed, embedded_model, model, has_thumb)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET
            filename=excluded.filename, source=excluded.source, folder=excluded.folder,
            day=excluded.day, ext=excluded.ext, size_bytes=excluded.size_bytes,
            mtime=excluded.mtime, sha256=excluded.sha256, width=excluded.width,
            height=excluded.height, prompt=excluded.prompt, seed=excluded.seed,
            embedded_model=excluded.embedded_model, model=excluded.model,
            has_thumb=excluded.has_thumb
        """,
        (
            relpath,
            _nfc(path.name),
            source,
            folder,
            day,
            path.suffix.lower().lstrip("."),
            st.st_size,
            st.st_mtime,
            sha,
            width,
            height,
            meta.prompt if meta else None,
            meta.seed if meta else None,
            meta.model if meta else None,
            meta.model if meta else None,
            int(has_thumb),
        ),
    )
    if old is None:
        report.artworks_new += 1
    else:
        report.artworks_updated += 1


def _ingest_checksums(conn: sqlite3.Connection, report: IngestReport, data_dir: Path) -> None:
    conn.execute("DELETE FROM checksums")
    conn.execute("UPDATE artworks SET checksum_status = NULL")
    art_dir = find_child(data_dir, CHECKSUMS[0])
    path = find_child(art_dir, CHECKSUMS[1]) if art_dir else None
    if path is None:
        report.missing.append("/".join(CHECKSUMS))
        return
    text = read_text(path, report, data_dir)
    if text is None:
        return
    sums, bad = parse_checksums(text)
    if bad:
        report.problem(rel(path, data_dir), f"нераспознанные строки: {', '.join(map(str, bad))}")
    conn.executemany(
        "INSERT OR REPLACE INTO checksums(path, sha256) VALUES (?, ?)",
        [(_nfc(p), s) for p, s in sums.items()],
    )
    rows = conn.execute("SELECT id, path, source, filename, sha256 FROM artworks").fetchall()
    by_path: dict[str, sqlite3.Row] = {}
    by_name: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_path[r["path"]] = r
        by_path.setdefault(r["path"].split("/", 1)[1], r)
        by_name[r["filename"]].append(r)
    art_root = _nfc(art_dir.name)
    for listed, expected in sums.items():
        key = _nfc(listed).removeprefix("./")
        row = by_path.get(f"{art_root}/{key}") or by_path.get(key)
        if row is None and len(by_name.get(Path(key).name, [])) == 1:
            row = by_name[Path(key).name][0]
        if row is None:
            report.checksums_missing_files.append(listed)
            continue
        ok = row["sha256"] == expected
        conn.execute(
            "UPDATE artworks SET checksum_status=? WHERE id=?",
            ("ok" if ok else "mismatch", row["id"]),
        )
        if ok:
            report.checksums_ok += 1
        else:
            report.checksum_mismatches.append(row["path"])


def _ingest_catalogue(conn: sqlite3.Connection, report: IngestReport, data_dir: Path) -> None:
    conn.execute("UPDATE artworks SET catalogue_id = NULL")
    conn.execute("DELETE FROM catalogue")
    art_dir = find_child(data_dir, CATALOGUE[0])
    path = find_child(art_dir, CATALOGUE[1]) if art_dir else None
    if path is None:
        report.missing.append("/".join(CATALOGUE))
    else:
        text = read_text(path, report, data_dir)
        entries = []
        if text is not None:
            try:
                entries = parse_catalogue(text)
            except Exception as exc:
                report.problem(rel(path, data_dir), f"не удалось разобрать каталог: {exc}")
        by_name: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in conn.execute("SELECT id, path, filename, day FROM artworks"):
            by_name[r["filename"].casefold()].append(r)
        for e in entries:
            cur = conn.execute(
                """INSERT INTO catalogue(position, day, title, filenames, format, width, height,
                                         size_text, model, seed, prose)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (e.position, e.day, e.title, "\n".join(e.filenames), e.format, e.width,
                 e.height, e.size_text, e.model, e.seed, e.prose),
            )
            cat_id = cur.lastrowid
            report.catalogue_entries += 1
            if not e.filenames:
                report.catalogue_unlinked.append(f"{e.day or '?'} — {e.title} (нет имени файла)")
                continue
            linked = False
            for fname in e.filenames:
                fname = _nfc(fname).strip().removeprefix("./")
                cands = by_name.get(Path(fname).name.casefold(), [])
                if "/" in fname:
                    by_suffix = [c for c in cands if c["path"].endswith(fname)]
                    cands = by_suffix or cands
                same_day = [c for c in cands if e.day and c["day"] == e.day]
                for c in same_day or cands:
                    conn.execute("UPDATE artworks SET catalogue_id=? WHERE id=?", (cat_id, c["id"]))
                    linked = True
            if linked:
                report.catalogue_linked += 1
            else:
                report.catalogue_unlinked.append(f"{e.day or '?'} — {', '.join(e.filenames)}")
    conn.execute(
        """UPDATE artworks SET model = COALESCE(
               embedded_model,
               (SELECT model FROM catalogue c WHERE c.id = artworks.catalogue_id))"""
    )


def _store_doc(conn, kind, slug, path, data_dir, title, day, parsed) -> int:
    cur = conn.execute(
        "INSERT INTO documents(kind, slug, path, title, day, preamble, mtime) "
        "VALUES (?,?,?,?,?,?,?)",
        (kind, slug, rel(path, data_dir), title, day, parsed.preamble, path.stat().st_mtime),
    )
    doc_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO sections(doc_id, position, anchor, day, title, body) VALUES (?,?,?,?,?,?)",
        [(doc_id, i, s.anchor, s.day, s.title or s.heading, s.body)
         for i, s in enumerate(parsed.sections)],
    )
    return doc_id


def _ingest_documents(conn: sqlite3.Connection, report: IngestReport, data_dir: Path) -> None:
    conn.execute("DELETE FROM sections")
    conn.execute("DELETE FROM documents")

    diary = find_child(data_dir, DIARY)
    if diary is None:
        report.missing.append(DIARY)
    else:
        text = read_text(diary, report, data_dir)
        if text is not None:
            parsed = split_sections(text)
            _store_doc(conn, "diary", "дневник", diary, data_dir, parsed.title or "Дневник",
                       None, parsed)
            report.diary_entries = len(parsed.sections)
            undated = [s.heading for s in parsed.sections if not s.day]
            if undated:
                report.problem(DIARY, f"записи без даты: {'; '.join(undated[:10])}")

    used_slugs = {"дневник", "гардероб"}
    stories_dir = find_child(data_dir, STORIES)
    if stories_dir is None or not stories_dir.is_dir():
        report.missing.append(f"{STORIES}/")
    else:
        for path in walk_files(stories_dir, {".md", ".markdown"}, report, data_dir):
            text = read_text(path, report, data_dir)
            if text is None:
                continue
            parsed = split_sections(text)
            stem = _nfc(path.stem)
            day = (
                valid_date(stem)
                or next((s.day for s in parsed.sections if s.day), None)
                or mtime_day(path.stat().st_mtime)
            )
            title = parsed.title or stem
            slug = unique_anchor(slugify(stem), used_slugs)
            _store_doc(conn, "story", slug, path, data_dir, title, day, parsed)
            report.stories += 1

    wardrobe = find_child(data_dir, WARDROBE)
    if wardrobe is None:
        report.missing.append(WARDROBE)
    else:
        text = read_text(wardrobe, report, data_dir)
        if text is not None:
            parsed = split_sections(text)
            _store_doc(conn, "wardrobe", "гардероб", wardrobe, data_dir,
                       parsed.title or "Гардероб", None, parsed)
            report.wardrobe = True


def _wav_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            return round(w.getnframes() / rate, 2) if rate else None
    except (wave.Error, EOFError, OSError):
        return None


def _ingest_audio(conn: sqlite3.Connection, report: IngestReport, data_dir: Path) -> None:
    seen: set[str] = set()
    for root_name in AUDIO_ROOTS:
        root = find_child(data_dir, root_name)
        if root is None or not root.is_dir():
            report.missing.append(f"{root_name}/")
            continue
        for path in walk_files(root, AUDIO_EXTS, report, data_dir):
            relpath = rel(path, data_dir)
            try:
                st = path.stat()
                parts = list(Path(rel(path, root)).parts)
                ext = path.suffix.lower().lstrip(".")
                duration = _wav_duration(path) if ext == "wav" else None
                conn.execute(
                    """INSERT INTO audio(path, filename, source, day, ext, size_bytes, mtime,
                                         duration)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(path) DO UPDATE SET filename=excluded.filename,
                         source=excluded.source, day=excluded.day, ext=excluded.ext,
                         size_bytes=excluded.size_bytes, mtime=excluded.mtime,
                         duration=excluded.duration""",
                    (relpath, _nfc(path.name), _nfc(root.name), derive_day(parts, st.st_mtime),
                     ext, st.st_size, st.st_mtime, duration),
                )
                seen.add(relpath)
            except Exception as exc:
                report.problem(relpath, f"{type(exc).__name__}: {exc}")
    for (p,) in conn.execute("SELECT path FROM audio").fetchall():
        if p not in seen:
            conn.execute("DELETE FROM audio WHERE path=?", (p,))
    report.audio = len(seen)


def _rebuild_search(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM search")
    conn.execute(
        """INSERT INTO search(kind, ref, day, title, body)
           SELECT 'art', a.id, a.day,
                  a.filename || ' ' || COALESCE(c.title, ''),
                  COALESCE(c.prose, '') || ' ' || COALESCE(a.prompt, '') || ' '
                  || COALESCE(a.model, '') || ' ' || a.folder
           FROM artworks a LEFT JOIN catalogue c ON c.id = a.catalogue_id"""
    )
    conn.execute(
        """INSERT INTO search(kind, ref, day, title, body)
           SELECT d.kind, d.slug || '#' || s.anchor, s.day, s.title, s.body
           FROM sections s JOIN documents d ON d.id = s.doc_id"""
    )
    conn.execute(
        """INSERT INTO search(kind, ref, day, title, body)
           SELECT d.kind, d.slug, d.day, d.title, d.preamble
           FROM documents d WHERE d.preamble != '' OR d.kind = 'story'"""
    )
