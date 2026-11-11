from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from conftest import make_png, make_wav
from fastapi.testclient import TestClient
from PIL import Image

from dom.config import Config
from dom.db import connect
from dom.ingest import THUMBS_VERSION, ingest, thumb_path
from dom.markup import render
from dom.parsing import parse_wardrobe
from dom.queries import LibraryFilters, library, text_hits
from dom.web import audio_title, create_app


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def names(conn, **kw) -> list[str]:
    rows, *_ = library(conn, LibraryFilters(**kw))
    return sorted(r["filename"] for r in rows)


# --- 1. library hygiene ------------------------------------------------------------------


def test_intermediates_and_reference_photos_are_excluded(tmp_path):
    d = tmp_path / "data"
    for name in ("a.png", "a_raw.png", "b_depth.png", "c_src_1.jpg", "d_raw.png", "e_MASK.png"):
        make_png(d / "arts" / name, (8, 8), "red")
    (d / "arts" / "notes.txt").write_text("не картинка", encoding="utf-8")
    cfg = Config(data_dir=d, db_path=tmp_path / "s" / "db", cache_dir=tmp_path / "c")
    conn = connect(cfg.db_path)
    r = ingest(cfg, conn)
    assert names(conn) == ["a.png", "d_raw.png"]
    assert r.excluded == {"raw": 1, "depth": 1, "src": 1, "mask": 1}
    assert r.to_dict()["excluded"] == r.excluded

    # once the final copy appears, the orphan raw is pruned
    make_png(d / "arts" / "d.png", (8, 8), "red")
    r2 = ingest(cfg, conn)
    assert names(conn) == ["a.png", "d.png"] and r2.artworks_removed == 1
    conn.close()

    with TestClient(create_app(cfg)) as c:
        assert c.get("/media/file/arts/a.png").status_code == 200
        for hidden in ("b_depth.png", "c_src_1.jpg", "a_raw.png", "d_raw.png"):
            assert c.get(f"/media/file/arts/{hidden}").status_code == 404
            assert c.get(f"/page/arts/{hidden}").status_code == 404


def test_cli_json_reports_excluded(cfg, data_dir, monkeypatch, capsys):
    from dom.__main__ import main

    make_png(data_dir / "arts" / "x_depth.png", (8, 8), "red")
    monkeypatch.setenv("DOM_DATA_DIR", str(cfg.data_dir))
    monkeypatch.setenv("DOM_DB_PATH", str(cfg.db_path))
    monkeypatch.setenv("DOM_CACHE_DIR", str(cfg.cache_dir))
    assert main(["ingest", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["excluded"] == {"depth": 1}


# --- 2. thumbnails -------------------------------------------------------------------------


def test_thumbnails_are_bigger_but_never_upscaled(cfg, conn, data_dir):
    make_png(data_dir / "arts" / "big.png", (2000, 1000), "green")
    ingest(cfg, conn)
    rows = dict(conn.execute("SELECT filename, sha256 FROM artworks").fetchall())
    with Image.open(thumb_path(cfg.cache_dir, rows["big.png"])) as im:
        assert im.size == (1280, 640)
    with Image.open(thumb_path(cfg.cache_dir, rows["one.png"])) as im:
        assert im.size == (64, 48)


def test_thumbs_version_change_rebuilds_cache(cfg, conn, ingested):
    assert conn.execute("SELECT value FROM meta WHERE key='thumbs_version'").fetchone()[0] == (
        THUMBS_VERSION
    )
    junk = cfg.cache_dir / "thumbs" / "00" / "stale.webp"
    junk.parent.mkdir(parents=True, exist_ok=True)
    junk.write_bytes(b"old")
    assert ingest(cfg, conn).thumbnails_created == 0  # same version: nothing to do
    with conn:
        conn.execute("UPDATE meta SET value='1' WHERE key='thumbs_version'")
    r = ingest(cfg, conn)
    assert r.thumbnails_created == 3 and not junk.exists()


def test_thumb_cache_headers_and_lightbox(client, conn):
    a = conn.execute("SELECT id, sha256 FROM artworks WHERE filename='one.png'").fetchone()
    t = client.get(f"/thumb/{a['sha256']}.webp")
    assert "max-age=31536000" in t.headers["cache-control"]
    page = client.get(f"/art/{a['id']}").text
    assert f'href="/media/art/{a["id"]}" data-lightbox' in page
    assert 'id="lightbox"' in page and "Escape" in page


# --- 3. captions ---------------------------------------------------------------------------


def write_captions(cfg: Config, data_dir: Path) -> None:
    one = sha(data_dir / "arts" / "2025-03-14" / "one.png")
    two = sha(data_dir / "арты" / "2025-03-15" / "two.png")
    lines = [
        {"sha256": one, "path": "x", "caption": "Девушка с фонарём", "who": "other", "model": "m"},
        {"sha256": two, "caption": "Пустая пристань в тумане", "who": "none", "ts": 1790000000},
        {"sha256": "f" * 64, "caption": "неизвестный арт", "who": "shinobu"},
    ]
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.captions_file.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8"
    )


def test_captions_sidecar(cfg, conn, data_dir):
    assert ingest(cfg, conn).captions == 0  # missing sidecar is fine
    write_captions(cfg, data_dir)
    r = ingest(cfg, conn)
    assert r.captions == 2
    rows = {
        x["filename"]: x
        for x in conn.execute("SELECT filename, caption, caption_who, caption_model FROM artworks")
    }
    assert rows["one.png"]["caption"] == "Девушка с фонарём"
    assert rows["one.png"]["caption_who"] == "other" and rows["one.png"]["caption_model"] == "m"
    assert rows["two.png"]["caption_who"] == "none"
    assert rows["three.png"]["caption"] is None
    # search reaches caption text, and the who filter works
    assert names(conn, q="фонарём") == ["one.png"]
    hit, *_ = library(conn, LibraryFilters(q="пристань"))[0]
    assert "<mark>пристань</mark>" in hit["snippet"].lower()
    assert names(conn, who="other") == ["one.png"]
    assert names(conn, who="none") == ["two.png"]
    assert names(conn, who="shinobu") == []


def test_captions_in_pages_and_stats(cfg, conn, data_dir):
    write_captions(cfg, data_dir)
    ingest(cfg, conn)
    with TestClient(create_app(cfg)) as c:
        lib = c.get("/").text
        assert "Девушка с фонарём" in lib and "не я" in lib and "без людей" in lib
        assert "Найдено артов: 1" in c.get("/", params={"who": "other"}).text
        aid = conn.execute("SELECT id FROM artworks WHERE filename='one.png'").fetchone()[0]
        assert '<p class="lead">Девушка с фонарём</p>' in c.get(f"/art/{aid}").text
        s = c.get("/api/stats").json()
        assert s["artworks_captioned"] == 2 and s["captions_by_who"] == {"other": 1, "none": 1}


# --- 4. stories: media references ------------------------------------------------------------

STORY_MEDIA = """# Колокол

Страница: `страницы/2026-11-07_колокол.html`

`звуки/колокол-которого-нет.ogg`

На стене табличка «штырь-в-прорези.png».

«фото.png»

![Окно](рассказы/окно.png)

рассказы/окно.png

Скрипт `скрипты/колокол-которого-нет.py` и заметка `другое.md`.

| вещь | где |
|---|---|
| колокол | <нигде> |
"""


def story_setup(data_dir: Path) -> None:
    (data_dir / "страницы").mkdir()
    (data_dir / "страницы" / "2026-11-07_колокол.html").write_text(
        "<!doctype html><svg></svg><script>1</script>", encoding="utf-8"
    )
    (data_dir / "страницы" / ".hidden.html").write_text("x", encoding="utf-8")
    make_wav(data_dir / "звуки" / "колокол-которого-нет.ogg")
    make_png(data_dir / "рассказы" / "окно.png", (8, 8), "white")
    make_png(data_dir / "рассказы" / "картинки" / "фото.png", (8, 8), "white")
    (data_dir / "рассказы" / "колокол.md").write_text(STORY_MEDIA, encoding="utf-8")


def test_story_media_render_and_routes(cfg, conn, data_dir):
    story_setup(data_dir)
    ingest(cfg, conn)
    with TestClient(create_app(cfg)) as c:
        html = c.get("/stories/колокол").text
        assert (
            '<iframe loading="lazy"' in html and 'sandbox="allow-scripts allow-same-origin"' in html
        )
        assert "интерактивная страница" in html and "открыть отдельно" in html
        assert '<audio controls preload="metadata" src="/media/file/' in html
        # guillemet reference to a file that does not exist
        assert "файл не найден" in html and "штырь-в-прорези.png</figcaption>" in html
        assert "«штырь-в-прорези.png»" not in html and "табличка.</p>" in html
        # found by basename deeper in the tree
        assert "/media/file/" + quote("рассказы/картинки/фото.png") in html
        assert "<figcaption>Окно</figcaption>" in html
        assert "<code>скрипты/колокол-которого-нет.py</code>" in html
        assert "<code>другое.md</code>" in html
        assert "<table><thead><tr><th>вещь</th>" in html and "&lt;нигде&gt;" in html

        r = c.get("/page/страницы/2026-11-07_колокол.html")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        assert r.headers["x-content-type-options"] == "nosniff"
        a = c.get("/media/file/звуки/колокол-которого-нет.ogg", headers={"Range": "bytes=0-3"})
        assert a.status_code == 206 and a.headers["content-type"] == "audio/ogg"
        for bad in (
            "/page/../etc/passwd",
            "/page/%2e%2e/%2e%2e/etc/passwd",
            "/page/страницы/.hidden.html",
            "/page/рассказы/колокол.md",
            "/media/file/страницы/2026-11-07_колокол.html",
        ):
            assert c.get(bad).status_code == 404, bad
        with (data_dir / "страницы" / "big.html").open("wb") as fh:
            fh.truncate(21 * 1024 * 1024)
        assert c.get("/page/страницы/big.html").status_code == 413


def test_markup_without_resolver():
    out = str(render("`звуки/x.mp3`\n\nтекст `a.py` и «b.webp»"))
    assert out.startswith('<figure class="media audio"><audio controls preload="metadata"')
    assert "<p>" not in out.split("</figure>")[0]
    assert "<code>a.py</code>" in out and '<img loading="lazy" src="/media/file/b.webp"' in out
    assert "<figure" not in str(render("`../etc/x.png`"))


# --- 5. voices -----------------------------------------------------------------------------


def test_voices_sidecar_and_page(cfg, conn, data_dir):
    make_wav(data_dir / "голосовые" / "2026-11-11.wav", seconds=1.0)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.voices_file.write_text(
        json.dumps(
            {
                "filename": "2025-03-14-утро.wav",
                "title": "Колокол, которого нет",
                "context": "голос из рассказа",
                "source_slug": "2025-03-15-лес",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ingest(cfg, conn)
    rows = {r["filename"]: r for r in conn.execute("SELECT * FROM audio")}
    morning = rows["2025-03-14-утро.wav"]
    assert morning["title"] == "Колокол, которого нет" and morning["context"] == "голос из рассказа"
    assert abs(morning["duration"] - 0.5) < 0.05
    assert rows["2026-11-11.wav"]["title"] is None
    assert audio_title(rows["2026-11-11.wav"]) == "Голосовое, 11 ноября"
    assert audio_title({"title": None, "filename": "2026-11-11.ogg", "source": "голосовые"}) == (
        "Голосовое, 11 ноября"
    )
    with TestClient(create_app(cfg)) as c:
        page = c.get("/audio").text
        assert "<strong" in page and "Колокол, которого нет" in page
        assert 'href="/stories/2025-03-15-лес">из рассказа „Лесная сказка“' in page
        assert "Голосовое, 11 ноября" in page and "0:01" in page


# --- 6. wardrobe ---------------------------------------------------------------------------

SECTIONS = ["Вечер", "Дом", "Улица", "Боевое / тьма", "Специальное"]


def wardrobe_md() -> str:
    out = ["# Гардероб", ""]
    n = 1
    for i, section in enumerate(SECTIONS):
        out += [f"## {section}", ""]
        for _ in range(3 if i < 2 else 2):
            out.append(f"**{n}. Образ {n}** — описание образа {n},")
            out.append("продолжение описания.")
            out.append(f"- промт: `prompt {n}, black dress`")
            if n == 1:
                out.append("- надето: 16.10, арт «Хозяйка сервера» (Sinister v9.0, seed 1)")
                out.append("- фото: two.png")
            elif n == 2:
                out.append("- надето: 14.03, арт «Кошка на крыше» (dreamy-xl)")
            elif n == 3:
                out.append("- надето: 01.01, арт «three»")
            out.append("")
            n += 1
    return "\n".join(out)


def test_parse_wardrobe_entries():
    items = parse_wardrobe(wardrobe_md())
    assert len(items) == 12
    assert [i.number for i in items] == [str(n) for n in range(1, 13)]
    assert {i.section for i in items} == set(SECTIONS)
    first = items[0]
    assert first.title == "Образ 1" and first.section == "Вечер"
    assert first.prose == "описание образа 1,\nпродолжение описания."
    assert first.prompt == "prompt 1, black dress"
    assert first.photo == "two.png" and first.art_title == "Хозяйка сервера"
    assert items[1].photo is None and items[1].art_title == "Кошка на крыше"
    assert items[4].worn_note is None and items[4].art_title is None


def test_wardrobe_page_and_art_resolution(cfg, conn, data_dir):
    (data_dir / "гардероб.md").write_text(wardrobe_md(), encoding="utf-8")
    r = ingest(cfg, conn)
    assert r.wardrobe_items == 12
    art = dict(conn.execute("SELECT id, filename FROM artworks").fetchall())
    by_number = dict(conn.execute("SELECT number, art_id FROM wardrobe_items").fetchall())
    assert art[by_number["1"]] == "two.png"  # фото: wins over арт «…»
    assert art[by_number["2"]] == "one.png"  # catalogue title
    assert art[by_number["3"]] == "three.png"  # file name without extension
    assert by_number["5"] is None
    assert not any(h["kind"] == "wardrobe" and not h["anchor"] for h in text_hits(conn, "образ"))
    with TestClient(create_app(cfg)) as c:
        page = c.get("/wardrobe").text
        assert page.count('class="card"') == 12
        assert page.count("фото ещё нет") == 9
        assert 'id="боевое-тьма"' in page and "Специальное" in page
        assert "<summary>промт</summary>" in page and "data-lightbox" in page


def test_wardrobe_search_links_to_cards(ingested, conn):
    hits = [h for h in text_hits(conn, "синее") if h["kind"] == "wardrobe"]
    assert hits and hits[0]["anchor"] == "w-0"
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE kind='wardrobe'").fetchone()[0] == 0


# --- 7. кладовая ---------------------------------------------------------------------------


def test_pick_page_explains_itself(client):
    page = client.get("/pick").text
    assert "Кладовая — это не архив и не полка." in page
    assert "в мешке сейчас 3 арта" in page
    client.post("/api/pick", params={"count": 2})
    assert "в мешке сейчас 1 арт<" in client.get("/pick").text
