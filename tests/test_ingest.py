from __future__ import annotations

import os
from pathlib import Path

import pytest

from dom.config import Config, ConfigError
from dom.db import connect
from dom.ingest import ingest, thumb_path
from dom.queries import LibraryFilters, library, text_hits


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


def test_ingest_counts(ingested, conn):
    r = ingested
    assert r.artworks == 3
    assert r.artworks_new == 3
    assert r.catalogue_entries == 3
    assert r.catalogue_linked == 2
    assert r.diary_entries == 2
    assert r.stories == 1
    assert r.audio == 1
    assert r.wardrobe is True
    assert r.thumbnails_created == 3
    assert r.missing == ["звуки/"]
    assert conn.execute("SELECT COUNT(*) FROM artworks").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM audio").fetchone()[0] == 1


def test_artwork_metadata_and_thumbnails(ingested, conn, cfg):
    one = conn.execute("SELECT * FROM artworks WHERE filename = 'one.png'").fetchone()
    assert (one["width"], one["height"]) == (64, 48)
    assert one["day"] == "2025-03-14"
    assert one["source"] == "arts"
    assert one["folder"] == "2025-03-14"
    assert one["seed"] == "12345"
    assert one["embedded_model"] == "dreamy-xl"
    assert one["prompt"].startswith("кот на крыше")
    assert len(one["sha256"]) == 64
    thumb = thumb_path(cfg.cache_dir, one["sha256"])
    assert thumb.is_file() and thumb.suffix == ".webp"
    loose = conn.execute("SELECT * FROM artworks WHERE filename = 'three.png'").fetchone()
    assert loose["folder"] == ""


def test_catalogue_linking(ingested, conn):
    rows = {
        r["filename"]: r
        for r in conn.execute(
            "SELECT a.filename, a.model, c.title, c.seed, c.width FROM artworks a "
            "LEFT JOIN catalogue c ON c.id = a.catalogue_id"
        )
    }
    assert rows["one.png"]["title"] == "Кошка на крыше"
    assert rows["two.png"]["title"] == "Утренний туман"
    assert rows["two.png"]["seed"] == "777"
    assert rows["two.png"]["width"] == 32
    # embedded model wins, catalogue model fills the gap
    assert rows["one.png"]["model"] == "dreamy-xl"
    assert rows["two.png"]["model"] == "aurora-v2"
    assert rows["three.png"]["title"] is None
    assert any("missing.png" in x for x in ingested.catalogue_unlinked)


def test_checksums(ingested, conn):
    status = dict(conn.execute("SELECT filename, checksum_status FROM artworks").fetchall())
    assert status == {"one.png": None, "two.png": "ok", "three.png": "mismatch"}
    assert ingested.checksums_ok == 1
    assert ingested.checksum_mismatches == ["арты/three.png"]
    assert any("нераспознанные строки" in p["error"] for p in ingested.problems)


def test_fts_finds_russian_words(ingested, conn):
    rows, total, _, _ = library(conn, LibraryFilters(q="кошка"))
    assert total == 1 and rows[0]["filename"] == "one.png"
    # whole-word match inside catalogue prose, case-insensitive
    rows, total, _, _ = library(conn, LibraryFilters(q="ТУМАН"))
    assert [r["filename"] for r in rows] == ["two.png"]
    # all words must match
    _, total, _, _ = library(conn, LibraryFilters(q="туман кошка"))
    assert total == 0
    hits = text_hits(conn, "бергамотом")
    assert len(hits) == 1
    assert hits[0]["kind"] == "diary" and hits[0]["anchor"] == "2025-03-14"
    assert "<mark>" in hits[0]["snippet"]
    assert any(h["kind"] == "story" for h in text_hits(conn, "боровик"))
    # FTS syntax characters in user input must not break the query
    library(conn, LibraryFilters(q='"кошка" OR (NEAR'))


def test_library_filters(ingested, conn):
    def names(**kw):
        rows, *_ = library(conn, LibraryFilters(**kw))
        return sorted(r["filename"] for r in rows)

    assert names(source="арты") == ["three.png", "two.png"]
    assert names(catalogued="yes") == ["one.png", "two.png"]
    assert names(catalogued="no") == ["three.png"]
    assert names(model="aurora-v2") == ["two.png"]
    assert names(date_from="2025-03-15", date_to="2025-03-15") == ["two.png"]


def test_data_dir_is_never_modified(cfg, conn, data_dir):
    before = snapshot(data_dir)
    ingest(cfg, conn)
    ingest(cfg, conn)
    assert snapshot(data_dir) == before


def test_reingest_is_incremental_and_handles_removals(cfg, conn, ingested, data_dir):
    r2 = ingest(cfg, conn)
    assert (r2.artworks_new, r2.artworks_updated, r2.thumbnails_created) == (0, 0, 0)
    (data_dir / "арты" / "three.png").unlink()  # the owner removes a file themselves
    r3 = ingest(cfg, conn)
    assert r3.artworks == 2 and r3.artworks_removed == 1


@pytest.mark.parametrize("variant", ["does-not-exist", "empty"])
def test_missing_directories_do_not_crash(tmp_path, variant):
    data = tmp_path / variant
    if variant == "empty":
        data.mkdir()
    cfg = Config(data_dir=data, db_path=tmp_path / "x.db", cache_dir=tmp_path / "c")
    conn = connect(cfg.db_path)
    r = ingest(cfg, conn)
    conn.close()
    assert r.artworks == 0 and r.stories == 0 and r.audio == 0
    assert "дневник.md" in r.missing and "arts/" in r.missing


def test_unparseable_files_are_reported(cfg, conn, data_dir):
    (data_dir / "arts" / "broken.png").write_bytes(b"definitely not a png")
    (data_dir / "рассказы" / "cp1251.md").write_bytes("# Привет\nтекст".encode("cp1251"))
    r = ingest(cfg, conn)
    paths = {p["path"] for p in r.problems}
    assert "arts/broken.png" in paths
    assert "рассказы/cp1251.md" in paths
    assert r.artworks == 4  # the broken file is still listed, just without a thumbnail
    assert r.stories == 2


def test_config_refuses_writes_into_data_dir(tmp_path):
    env = {"DOM_DATA_DIR": str(tmp_path), "DOM_DB_PATH": str(tmp_path / "x.db")}
    with pytest.raises(ConfigError):
        Config.from_env(env)
    cfg = Config.from_env({"DOM_DATA_DIR": str(tmp_path / "d"), "DOM_PORT": "9001"})
    assert cfg.port == 9001 and cfg.pick_cooldown_days == 5
