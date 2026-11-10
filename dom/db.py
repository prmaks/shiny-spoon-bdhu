"""SQLite schema and connection helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS artworks (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,          -- relative to the data dir, posix style
    filename TEXT NOT NULL,
    source TEXT NOT NULL,               -- top-level folder: arts / арты
    folder TEXT NOT NULL DEFAULT '',    -- dated sub-folder (session), '' for loose files
    day TEXT NOT NULL,                  -- YYYY-MM-DD (folder, filename or mtime)
    ext TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime REAL NOT NULL,
    sha256 TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    prompt TEXT,
    seed TEXT,
    model TEXT,                         -- embedded model, else catalogue model
    embedded_model TEXT,
    catalogue_id INTEGER REFERENCES catalogue(id) ON DELETE SET NULL,
    has_thumb INTEGER NOT NULL DEFAULT 0,
    checksum_status TEXT                -- ok / mismatch / NULL (unknown)
);
CREATE INDEX IF NOT EXISTS ix_art_order ON artworks(day DESC, path);
CREATE INDEX IF NOT EXISTS ix_art_source ON artworks(source, day DESC, path);
CREATE INDEX IF NOT EXISTS ix_art_model ON artworks(model);
CREATE INDEX IF NOT EXISTS ix_art_cat ON artworks(catalogue_id);
CREATE INDEX IF NOT EXISTS ix_art_filename ON artworks(filename);
CREATE INDEX IF NOT EXISTS ix_art_session ON artworks(source, folder, day);

CREATE TABLE IF NOT EXISTS catalogue (
    id INTEGER PRIMARY KEY,
    position INTEGER NOT NULL,
    day TEXT,
    title TEXT NOT NULL,
    filenames TEXT NOT NULL DEFAULT '',  -- newline separated
    format TEXT,
    width INTEGER,
    height INTEGER,
    size_text TEXT,
    model TEXT,
    seed TEXT,
    prose TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                 -- diary / story / wardrobe
    slug TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    day TEXT,
    preamble TEXT NOT NULL DEFAULT '',
    mtime REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_doc_kind ON documents(kind, day DESC);

CREATE TABLE IF NOT EXISTS sections (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    anchor TEXT NOT NULL,
    day TEXT,
    title TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sec_doc ON sections(doc_id, position);
CREATE INDEX IF NOT EXISTS ix_sec_day ON sections(day);

CREATE TABLE IF NOT EXISTS audio (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    source TEXT NOT NULL,
    day TEXT NOT NULL,
    ext TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime REAL NOT NULL,
    duration REAL
);
CREATE INDEX IF NOT EXISTS ix_audio_day ON audio(day DESC, path);

CREATE TABLE IF NOT EXISTS checksums (
    path TEXT PRIMARY KEY,              -- as written in the file
    sha256 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS picks (
    id INTEGER PRIMARY KEY,
    batch TEXT NOT NULL,
    artwork_id INTEGER NOT NULL REFERENCES artworks(id) ON DELETE CASCADE,
    picked_at TEXT NOT NULL             -- ISO 8601 UTC
);
CREATE INDEX IF NOT EXISTS ix_picks_time ON picks(picked_at);
CREATE INDEX IF NOT EXISTS ix_picks_art ON picks(artwork_id, picked_at);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    kind UNINDEXED,
    ref UNINDEXED,
    day UNINDEXED,
    title,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def connect(db_path: Path | str, *, init: bool = True) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if init:
        conn.executescript(SCHEMA)
    return conn
