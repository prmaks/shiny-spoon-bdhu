"""Read-side queries used by the web UI and the CLI."""

from __future__ import annotations

import html
import json
import re
import sqlite3
from dataclasses import dataclass

PER_PAGE = 60
TIMELINE_DAYS = 14
TIMELINE_ARTS_PER_DAY = 24

_WORD = re.compile(r"\w+", re.UNICODE)


def fts_query(q: str | None) -> str | None:
    """Turn user input into a safe FTS5 query: every word must match (prefix match)."""
    words = _WORD.findall(q or "")
    if not words:
        return None
    return " ".join(f'"{w}"*' for w in words[:12])


@dataclass
class LibraryFilters:
    q: str = ""
    date_from: str = ""
    date_to: str = ""
    source: str = ""
    catalogued: str = ""  # "", "yes", "no"
    model: str = ""
    page: int = 1

    def where(self) -> tuple[list[str], list]:
        clauses: list[str] = []
        params: list = []
        if self.date_from:
            clauses.append("a.day >= ?")
            params.append(self.date_from)
        if self.date_to:
            clauses.append("a.day <= ?")
            params.append(self.date_to)
        if self.source:
            clauses.append("a.source = ?")
            params.append(self.source)
        if self.catalogued == "yes":
            clauses.append("a.catalogue_id IS NOT NULL")
        elif self.catalogued == "no":
            clauses.append("a.catalogue_id IS NULL")
        if self.model:
            clauses.append("a.model = ?")
            params.append(self.model)
        return clauses, params


ART_COLUMNS = """a.id, a.path, a.filename, a.source, a.folder, a.day, a.width, a.height,
                 a.sha256, a.has_thumb, a.model, a.catalogue_id, c.title AS cat_title"""


def library(conn: sqlite3.Connection, f: LibraryFilters, per_page: int = PER_PAGE):
    clauses, params = f.where()
    match = fts_query(f.q)
    join = ""
    order = "a.day DESC, a.path"
    if match:
        join = (
            "JOIN (SELECT CAST(ref AS INTEGER) AS art_id, rank FROM search "
            "WHERE search MATCH ? AND kind = 'art') s ON s.art_id = a.id"
        )
        params = [match, *params]
        order = "s.rank, a.day DESC, a.path"
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    total = conn.execute(f"SELECT COUNT(*) FROM artworks a {join} {where}", params).fetchone()[0]
    pages = max(1, -(-total // per_page))
    page = min(max(1, f.page), pages)
    rows = conn.execute(
        f"""SELECT {ART_COLUMNS} FROM artworks a {join}
            LEFT JOIN catalogue c ON c.id = a.catalogue_id
            {where} ORDER BY {order} LIMIT ? OFFSET ?""",
        [*params, per_page, (page - 1) * per_page],
    ).fetchall()
    return rows, total, page, pages


def text_hits(conn: sqlite3.Connection, q: str | None, limit: int = 20) -> list[dict]:
    """Search diary, stories and wardrobe. Snippets are HTML-escaped with <mark> highlights."""
    match = fts_query(q)
    if not match:
        return []
    rows = conn.execute(
        """SELECT kind, ref, day, title,
                  snippet(search, 4, char(2), char(3), '…', 16) AS snip
           FROM search WHERE search MATCH ? AND kind != 'art' ORDER BY rank LIMIT ?""",
        (match, limit),
    ).fetchall()
    hits = []
    for r in rows:
        snip = html.escape(r["snip"] or "").replace("\x02", "<mark>").replace("\x03", "</mark>")
        slug, _, anchor = r["ref"].partition("#")
        hits.append(
            {"kind": r["kind"], "slug": slug, "anchor": anchor, "day": r["day"],
             "title": r["title"], "snippet": snip}
        )
    return hits


def facets(conn: sqlite3.Connection) -> dict:
    return {
        "sources": [r[0] for r in conn.execute("SELECT DISTINCT source FROM artworks ORDER BY 1")],
        "models": [
            (r[0], r[1])
            for r in conn.execute(
                "SELECT model, COUNT(*) FROM artworks WHERE model IS NOT NULL "
                "GROUP BY model ORDER BY 2 DESC, 1"
            )
        ],
    }


def artwork(conn: sqlite3.Connection, art_id: int):
    row = conn.execute(
        """SELECT a.*, c.title AS cat_title, c.prose AS cat_prose, c.model AS cat_model,
                  c.seed AS cat_seed, c.format AS cat_format, c.width AS cat_width,
                  c.height AS cat_height, c.size_text AS cat_size, c.day AS cat_day
           FROM artworks a LEFT JOIN catalogue c ON c.id = a.catalogue_id WHERE a.id = ?""",
        (art_id,),
    ).fetchone()
    if row is None:
        return None, [], None, None
    related = conn.execute(
        f"""SELECT {ART_COLUMNS} FROM artworks a LEFT JOIN catalogue c ON c.id = a.catalogue_id
            WHERE a.day = ? AND a.id != ? ORDER BY (a.source = ? AND a.folder = ?) DESC, a.path
            LIMIT 48""",
        (row["day"], row["id"], row["source"], row["folder"]),
    ).fetchall()
    # Library order is (day DESC, path ASC); "prev" is the newer neighbour.
    prev = conn.execute(
        """SELECT id, filename FROM artworks WHERE day > ? OR (day = ? AND path < ?)
           ORDER BY day ASC, path DESC LIMIT 1""",
        (row["day"], row["day"], row["path"]),
    ).fetchone()
    nxt = conn.execute(
        """SELECT id, filename FROM artworks WHERE day < ? OR (day = ? AND path > ?)
           ORDER BY day DESC, path ASC LIMIT 1""",
        (row["day"], row["day"], row["path"]),
    ).fetchone()
    return row, related, prev, nxt


def timeline(conn: sqlite3.Connection, page: int = 1, days_per_page: int = TIMELINE_DAYS):
    day_sql = """
        SELECT day FROM artworks
        UNION SELECT s.day FROM sections s JOIN documents d ON d.id = s.doc_id
              WHERE d.kind = 'diary' AND s.day IS NOT NULL
        UNION SELECT day FROM documents WHERE kind = 'story' AND day IS NOT NULL
    """
    total_days = conn.execute(f"SELECT COUNT(*) FROM ({day_sql})").fetchone()[0]
    pages = max(1, -(-total_days // days_per_page))
    page = min(max(1, page), pages)
    days = [
        r[0]
        for r in conn.execute(
            f"SELECT day FROM ({day_sql}) ORDER BY day DESC LIMIT ? OFFSET ?",
            (days_per_page, (page - 1) * days_per_page),
        )
    ]
    if not days:
        return [], page, pages
    marks = ",".join("?" * len(days))
    groups = {d: {"day": d, "diary": [], "stories": [], "arts": [], "arts_total": 0} for d in days}
    for r in conn.execute(
        f"""SELECT s.anchor, s.day, s.title, s.body FROM sections s
            JOIN documents d ON d.id = s.doc_id
            WHERE d.kind = 'diary' AND s.day IN ({marks}) ORDER BY s.position""",
        days,
    ):
        groups[r["day"]]["diary"].append(r)
    for r in conn.execute(
        f"SELECT slug, title, day, preamble FROM documents WHERE kind='story' AND day IN ({marks})",
        days,
    ):
        groups[r["day"]]["stories"].append(r)
    for r in conn.execute(
        f"""SELECT * FROM (
                SELECT a.id, a.filename, a.day, a.sha256, a.has_thumb, a.width, a.height,
                       c.title AS cat_title,
                       ROW_NUMBER() OVER (PARTITION BY a.day ORDER BY a.path) AS rn,
                       COUNT(*) OVER (PARTITION BY a.day) AS cnt
                FROM artworks a LEFT JOIN catalogue c ON c.id = a.catalogue_id
                WHERE a.day IN ({marks}))
            WHERE rn <= ? ORDER BY day DESC, rn""",
        [*days, TIMELINE_ARTS_PER_DAY],
    ):
        g = groups[r["day"]]
        g["arts"].append(r)
        g["arts_total"] = r["cnt"]
    return [groups[d] for d in days], page, pages


def document(conn: sqlite3.Connection, slug: str):
    doc = conn.execute("SELECT * FROM documents WHERE slug = ?", (slug,)).fetchone()
    if doc is None:
        return None, []
    sections = conn.execute(
        "SELECT * FROM sections WHERE doc_id = ? ORDER BY position", (doc["id"],)
    ).fetchall()
    return doc, sections


def documents_of_kind(conn: sqlite3.Connection, kind: str):
    return conn.execute(
        """SELECT d.*, (SELECT COUNT(*) FROM sections s WHERE s.doc_id = d.id) AS n_sections
           FROM documents d WHERE kind = ? ORDER BY day DESC, title""",
        (kind,),
    ).fetchall()


def audio_list(conn: sqlite3.Connection):
    return conn.execute("SELECT * FROM audio ORDER BY day DESC, path").fetchall()


def stats(conn: sqlite3.Connection) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    last = conn.execute("SELECT value FROM meta WHERE key = 'last_ingest'").fetchone()
    return {
        "artworks": one("SELECT COUNT(*) FROM artworks"),
        "artworks_catalogued": one("SELECT COUNT(*) FROM artworks WHERE catalogue_id IS NOT NULL"),
        "catalogue_entries": one("SELECT COUNT(*) FROM catalogue"),
        "diary_entries": one(
            "SELECT COUNT(*) FROM sections s JOIN documents d ON d.id = s.doc_id "
            "WHERE d.kind = 'diary'"
        ),
        "stories": one("SELECT COUNT(*) FROM documents WHERE kind = 'story'"),
        "audio": one("SELECT COUNT(*) FROM audio"),
        "picks": one("SELECT COUNT(*) FROM picks"),
        "first_day": one("SELECT MIN(day) FROM artworks"),
        "last_day": one("SELECT MAX(day) FROM artworks"),
        "bytes": one("SELECT COALESCE(SUM(size_bytes), 0) FROM artworks")
        + one("SELECT COALESCE(SUM(size_bytes), 0) FROM audio"),
        "last_ingest": json.loads(last[0]) if last else None,
    }
