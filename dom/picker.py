"""The "Кладовая" picker: random artworks with a per-artwork cooldown."""

from __future__ import annotations

import random
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(TS_FORMAT)


def _art_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "path": row["path"],
        "filename": row["filename"],
        "day": row["day"],
        "title": row["cat_title"],
        "model": row["model"],
        "url": f"/art/{row['id']}",
        "image": f"/media/art/{row['id']}",
        "thumb": f"/thumb/{row['sha256']}.webp" if row["has_thumb"] else None,
    }


def pick(
    conn: sqlite3.Connection,
    count: int,
    cooldown_days: int,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> dict:
    """Draw up to ``count`` artworks not picked within the cooldown window.

    Artworks on cooldown are excluded; only when *every* artwork is on cooldown is the
    whole collection used as the pool again. The pick is recorded in ``picks``.
    """
    now = now or datetime.now(UTC)
    rng = rng or random.SystemRandom()
    count = max(1, int(count))
    cutoff = _ts(now - timedelta(days=cooldown_days))
    eligible = [
        r[0]
        for r in conn.execute(
            """SELECT id FROM artworks WHERE id NOT IN
               (SELECT artwork_id FROM picks WHERE picked_at > ?)""",
            (cutoff,),
        )
    ]
    all_on_cooldown = not eligible
    pool = eligible or [r[0] for r in conn.execute("SELECT id FROM artworks")]
    chosen = rng.sample(pool, min(count, len(pool)))
    batch = uuid.uuid4().hex
    picked_at = _ts(now)
    with conn:
        conn.executemany(
            "INSERT INTO picks(batch, artwork_id, picked_at) VALUES (?, ?, ?)",
            [(batch, art_id, picked_at) for art_id in chosen],
        )
    return {
        "batch": batch,
        "picked_at": picked_at,
        "cooldown_days": cooldown_days,
        "requested": count,
        "eligible": len(eligible),
        "all_on_cooldown": all_on_cooldown and bool(pool),
        "artworks": _load(conn, chosen),
    }


def _load(conn: sqlite3.Connection, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    rows = conn.execute(
        f"""SELECT a.id, a.path, a.filename, a.day, a.model, a.sha256, a.has_thumb,
                   c.title AS cat_title
            FROM artworks a LEFT JOIN catalogue c ON c.id = a.catalogue_id
            WHERE a.id IN ({','.join('?' * len(ids))})""",
        ids,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [_art_dict(by_id[i]) for i in ids if i in by_id]


def last_pick(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT batch, picked_at FROM picks ORDER BY picked_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    ids = [
        r[0]
        for r in conn.execute("SELECT artwork_id FROM picks WHERE batch = ? ORDER BY id", (row[0],))
    ]
    return {"batch": row[0], "picked_at": row[1], "artworks": _load(conn, ids)}


def history(conn: sqlite3.Connection, limit: int = 30) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT p.picked_at, p.batch, a.id, a.filename FROM picks p
           JOIN artworks a ON a.id = p.artwork_id ORDER BY p.picked_at DESC, p.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
