"""FastAPI application: HTML pages, JSON endpoints and media serving."""

import contextlib
import re
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import markup, picker, queries
from .config import Config
from .db import connect
from .ingest import IngestBusy, ingest, thumb_path

HERE = Path(__file__).parent
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "ogg": "audio/ogg",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "flac": "audio/flac",
}
MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
KIND_LABELS = {"diary": "Дневник", "story": "Рассказ", "wardrobe": "Гардероб"}


def ru_date(value: str | None, weekday: bool = False) -> str:
    if not value:
        return ""
    try:
        d = date.fromisoformat(value[:10])
    except ValueError:
        return value
    text = f"{d.day} {MONTHS[d.month - 1]} {d.year}"
    return f"{text}, {WEEKDAYS[d.weekday()]}" if weekday else text


def filesize(n: int | None) -> str:
    if n is None:
        return "—"
    size = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return str(n)


def duration(sec: float | None) -> str:
    if not sec:
        return ""
    m, s = divmod(round(sec), 60)
    return f"{m}:{s:02d}"


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()
    connect(cfg.db_path).close()  # create the schema once at startup
    app = FastAPI(title="Дом Шинобу", docs_url=None, redoc_url=None)
    app.state.cfg = cfg
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.filters.update(ru_date=ru_date, filesize=filesize, duration=duration)
    env.globals.update(md=markup.render, excerpt=markup.excerpt, kind_labels=KIND_LABELS)

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = connect(cfg.db_path, init=False)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    def page(request: Request, name: str, **ctx):
        def url_with(**params) -> str:
            merged = {**dict(request.query_params), **params}
            clean = {k: v for k, v in merged.items() if v not in (None, "")}
            return f"{request.url.path}?{urlencode(clean)}" if clean else request.url.path

        return templates.TemplateResponse(request, name, {"url_with": url_with, **ctx})

    def safe_media_path(relpath: str) -> Path:
        root = cfg.data_dir.resolve()
        full = (root / relpath).resolve()
        if not full.is_relative_to(root) or not full.is_file():
            raise HTTPException(404, "Файл не найден")
        return full

    # --- pages -------------------------------------------------------------------------

    @app.get("/")
    def library_page(
        request: Request,
        conn: Conn,
        q: str = "",
        date_from: str = "",
        date_to: str = "",
        source: str = "",
        catalogued: str = "",
        model: str = "",
        page_no: Annotated[int, Query(alias="page", ge=1)] = 1,
    ):
        f = queries.LibraryFilters(q, date_from, date_to, source, catalogued, model, page_no)
        rows, total, cur, pages = queries.library(conn, f)
        return page(
            request,
            "library.html",
            f=f,
            rows=rows,
            total=total,
            page=cur,
            pages=pages,
            hits=queries.text_hits(conn, q) if q and cur == 1 else [],
            facets=queries.facets(conn),
        )

    @app.get("/art/{art_id}")
    def artwork_page(request: Request, conn: Conn, art_id: int):
        row, related, prev, nxt = queries.artwork(conn, art_id)
        if row is None:
            raise HTTPException(404, "Арт не найден")
        diary = conn.execute(
            """SELECT s.anchor, s.title FROM sections s JOIN documents d ON d.id = s.doc_id
               WHERE d.kind = 'diary' AND s.day = ? ORDER BY s.position""",
            (row["day"],),
        ).fetchall()
        return page(
            request, "artwork.html", a=row, related=related, prev=prev, next=nxt, diary=diary
        )

    @app.get("/timeline")
    def timeline_page(
        request: Request, conn: Conn, page_no: Annotated[int, Query(alias="page", ge=1)] = 1
    ):
        groups, cur, pages = queries.timeline(conn, page_no)
        return page(request, "timeline.html", groups=groups, page=cur, pages=pages)

    def reading(request: Request, conn: sqlite3.Connection, slug: str):
        doc, sections = queries.document(conn, slug)
        if doc is None:
            return page(request, "empty.html", title="Пока пусто")
        return page(request, "reading.html", doc=doc, sections=sections)

    @app.get("/diary")
    def diary_page(request: Request, conn: Conn):
        return reading(request, conn, "дневник")

    @app.get("/wardrobe")
    def wardrobe_page(request: Request, conn: Conn):
        return reading(request, conn, "гардероб")

    @app.get("/stories")
    def stories_page(request: Request, conn: Conn):
        return page(request, "stories.html", stories=queries.documents_of_kind(conn, "story"))

    @app.get("/stories/{slug}")
    def story_page(request: Request, conn: Conn, slug: str):
        doc, sections = queries.document(conn, slug)
        if doc is None or doc["kind"] != "story":
            raise HTTPException(404, "Рассказ не найден")
        return page(request, "reading.html", doc=doc, sections=sections)

    @app.get("/audio")
    def audio_page(request: Request, conn: Conn):
        return page(request, "audio.html", items=queries.audio_list(conn))

    @app.get("/pick")
    def pick_page(request: Request, conn: Conn):
        return page(
            request,
            "pick.html",
            result=picker.last_pick(conn),
            history=picker.history(conn),
            cooldown=cfg.pick_cooldown_days,
        )

    @app.post("/pick")
    def pick_form(conn: Conn, count: Annotated[int, Form(ge=1, le=100)] = 3):
        picker.pick(conn, count, cfg.pick_cooldown_days)
        return RedirectResponse("/pick", status_code=303)

    @app.get("/stats")
    def stats_page(request: Request, conn: Conn):
        return page(request, "stats.html", s=queries.stats(conn), cfg=cfg)

    @app.post("/ingest")
    def ingest_form(conn: Conn):
        with contextlib.suppress(IngestBusy):
            ingest(cfg, conn)
        return RedirectResponse("/stats", status_code=303)

    # --- JSON API ------------------------------------------------------------------------

    @app.post("/api/pick")
    def api_pick(conn: Conn, count: Annotated[int, Query(ge=1, le=100)] = 1):
        return picker.pick(conn, count, cfg.pick_cooldown_days)

    @app.post("/api/ingest")
    def api_ingest(conn: Conn):
        try:
            return ingest(cfg, conn).to_dict()
        except IngestBusy as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.get("/api/stats")
    def api_stats(conn: Conn):
        return queries.stats(conn)

    # --- media -----------------------------------------------------------------------------

    @app.get("/media/art/{art_id}")
    def media_art(conn: Conn, art_id: int):
        row = conn.execute("SELECT path, ext FROM artworks WHERE id = ?", (art_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Арт не найден")
        return FileResponse(safe_media_path(row["path"]), media_type=MEDIA_TYPES.get(row["ext"]))

    @app.get("/media/audio/{audio_id}")
    def media_audio(conn: Conn, audio_id: int):
        row = conn.execute("SELECT path, ext FROM audio WHERE id = ?", (audio_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Запись не найдена")
        return FileResponse(safe_media_path(row["path"]), media_type=MEDIA_TYPES.get(row["ext"]))

    @app.get("/thumb/{sha}.webp")
    def thumb(sha: str):
        if not SHA_RE.match(sha):
            raise HTTPException(404)
        path = thumb_path(cfg.cache_dir, sha)
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(
            path,
            media_type="image/webp",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    return app


def app_from_env() -> FastAPI:
    """Factory for ``uvicorn --factory dom.web:app_from_env``."""
    return create_app()
