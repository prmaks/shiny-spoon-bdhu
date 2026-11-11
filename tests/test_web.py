from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from dom.config import Config
from dom.db import connect
from dom.web import create_app


def art_id(conn, name):
    return conn.execute("SELECT id FROM artworks WHERE filename = ?", (name,)).fetchone()[0]


def test_audio_range_request_returns_206(client, conn):
    aid = conn.execute("SELECT id FROM audio").fetchone()[0]
    full = client.get(f"/media/audio/{aid}")
    assert full.status_code == 200
    assert full.headers["accept-ranges"] == "bytes"
    r = client.get(f"/media/audio/{aid}", headers={"Range": "bytes=10-29"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 10-29/{len(full.content)}"
    assert r.content == full.content[10:30]
    assert r.headers["content-type"].startswith("audio/")


def test_image_range_and_thumbnail(client, conn):
    aid = art_id(conn, "one.png")
    r = client.get(f"/media/art/{aid}", headers={"Range": "bytes=0-7"})
    assert r.status_code == 206 and r.content == b"\x89PNG\r\n\x1a\n"
    sha = conn.execute("SELECT sha256 FROM artworks WHERE id = ?", (aid,)).fetchone()[0]
    t = client.get(f"/thumb/{sha}.webp")
    assert t.status_code == 200 and t.headers["content-type"] == "image/webp"
    assert client.get("/thumb/nothex.webp").status_code == 404
    assert client.get("/media/art/999999").status_code == 404


def test_library_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Найдено артов: 3" in r.text
    r = client.get("/", params={"q": "бергамотом"})
    assert "В текстах" in r.text and "Ничего не найдено" in r.text
    r = client.get("/", params={"q": "кошка"})
    assert "Найдено артов: 1" in r.text and "Кошка на крыше" in r.text


def test_artwork_page(client, conn):
    r = client.get(f"/art/{art_id(conn, 'one.png')}")
    assert r.status_code == 200
    assert "Рыжая кошка сидит" in r.text
    assert "dreamy-xl" in r.text
    assert "/diary#2025-03-14" in r.text
    r = client.get(f"/art/{art_id(conn, 'two.png')}")
    assert 'rel="prev"' in r.text and 'rel="next"' in r.text
    assert client.get("/art/424242").status_code == 404


def test_timeline_and_reading(client):
    r = client.get("/timeline")
    assert r.status_code == 200
    assert 'id="d-2025-03-15"' in r.text and 'id="d-2025-03-14"' in r.text
    assert r.text.index('id="d-2025-03-15"') < r.text.index('id="d-2025-03-14"')
    assert "Лесная сказка" in r.text and "бергамотом" in r.text
    r = client.get("/diary")
    assert 'id="2025-03-14"' in r.text and "<strong>бергамотом</strong>" in r.text
    assert "Лесная сказка" in client.get("/stories").text
    r = client.get("/stories/2025-03-15-лес")
    assert r.status_code == 200 and 'id="глава-первая"' in r.text
    assert "Синее платье" in client.get("/wardrobe").text
    assert "<audio" in client.get("/audio").text


def test_pick_endpoints(client):
    r = client.post("/api/pick", params={"count": 2})
    assert r.status_code == 200
    data = r.json()
    assert len(data["artworks"]) == 2 and data["cooldown_days"] == 5
    r = client.post("/pick", data={"count": "1"}, follow_redirects=True)
    assert r.status_code == 200 and "Вытянуто" in r.text


def test_ingest_and_stats_endpoints(client):
    r = client.post("/api/ingest")
    assert r.status_code == 200 and r.json()["artworks"] == 3
    s = client.get("/api/stats").json()
    assert s["artworks"] == 3 and s["stories"] == 1
    page = client.get("/stats")
    assert "missing.png" in page.text and "three.png" in page.text


@pytest.mark.parametrize(
    "url", ["/", "/timeline", "/diary", "/stories", "/audio", "/pick", "/stats", "/wardrobe"]
)
def test_pages_render_with_empty_database(tmp_path, url):
    cfg = Config(data_dir=tmp_path / "nope", db_path=tmp_path / "e.db", cache_dir=tmp_path / "c")
    with TestClient(create_app(cfg)) as c:
        assert c.get(url).status_code == 200


def test_library_with_5000_artworks_is_fast(tmp_path):
    cfg = Config(data_dir=tmp_path / "d", db_path=tmp_path / "big.db", cache_dir=tmp_path / "c")
    conn = connect(cfg.db_path)
    rows = []
    for i in range(5000):
        day = f"2024-{1 + i % 12:02d}-{1 + i % 28:02d}"
        rows.append(
            (
                f"arts/{day}/img{i:05d}.png",
                f"img{i:05d}.png",
                "arts",
                day,
                day,
                "png",
                1000,
                0.0,
                f"{i:064x}",
                512,
                512,
                f"model-{i % 7}",
                1,
            )
        )
    with conn:
        conn.executemany(
            "INSERT INTO artworks(path, filename, source, folder, day, ext, size_bytes, mtime, "
            "sha256, width, height, model, has_thumb) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.execute(
            "INSERT INTO search(kind, ref, day, title, body) "
            "SELECT 'art', id, day, filename, 'описание ' || model FROM artworks"
        )
    conn.close()
    with TestClient(create_app(cfg)) as c:
        c.get("/")  # warm up template compilation
        for url in ("/", "/?page=40", "/?model=model-3&source=arts", "/?q=описание", "/timeline"):
            t = time.perf_counter()
            r = c.get(url)
            elapsed = time.perf_counter() - t
            assert r.status_code == 200
            assert elapsed < 0.3, f"{url} took {elapsed * 1000:.0f} ms"
