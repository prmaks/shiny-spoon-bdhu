from __future__ import annotations

import pytest

from dom.ingest import ingest
from dom.markup import render
from dom.parsing import extract_embedded, parse_catalogue, split_sections


def test_catalogue_meta_line_variants():
    text = (
        "## 2025-01-02 — Альфа\n`a.png` · PNG 1024×768 · 1,2 МБ · flux-dev, seed 42\n\nПроза.\n"
        "## 2025-01-03 – Бета\nпросто текст без метаданных\n"
        "## не дата\n`b.jpg` · JPEG 10x10\n"
    )
    a, b, c = parse_catalogue(text)
    assert (a.day, a.title, a.filenames) == ("2025-01-02", "Альфа", ["a.png"])
    assert (a.format, a.width, a.height, a.size_text) == ("PNG", 1024, 768, "1,2 МБ")
    assert (a.model, a.seed, a.prose) == ("flux-dev", "42", "Проза.")
    assert b.day == "2025-01-03" and b.filenames == [] and "без метаданных" in b.prose
    assert c.day is None and c.filenames == ["b.jpg"] and c.width == 10


def test_sections_unique_anchors():
    doc = split_sections("## 2025-01-01 — а\nx\n## 2025-01-01 — б\ny\n```\n## не раздел\n```\n")
    assert [s.anchor for s in doc.sections] == ["2025-01-01", "2025-01-01-2"]
    assert "## не раздел" in doc.sections[1].body


@pytest.mark.parametrize(
    ("heading", "day", "title"),
    [
        ("16 октября 2026", "2026-10-16", ""),
        ("1 мая 2026 г.", "2026-05-01", ""),
        ("3 сентября 2026, среда", "2026-09-03", ""),
        ("16 октября 2026 — первый снег", "2026-10-16", "первый снег"),
        ("3 ноября 2026, поздний вечер", "2026-11-03", "поздний вечер"),
        (
            "31.10.2026, 05:10 — взрослый японский референс",
            "2026-10-31",
            "взрослый японский референс",
        ),
        ("31.10.2026", "2026-10-31", ""),
        ("05.01.2026 г. · утро", "2026-01-05", "утро"),
    ],
)
def test_russian_dated_headings(heading, day, title):
    doc = split_sections(f"# Дневник\n\n## {heading}\nТекст записи.\n")
    (s,) = doc.sections
    assert (s.day, s.title, s.anchor) == (day, title, day)
    assert s.body == "Текст записи."


def test_full_russian_heading_keeps_title():
    (s,) = split_sections("## 16 октября 2026 — первый снег\nСнег.\n").sections
    assert s.day == "2026-10-16"
    assert s.title == "первый снег"


def test_russian_dated_heading_rejects_invalid_dates():
    doc = split_sections("## 31 февраля 2026\nx\n## 16 октябряя 2026\ny\n## 32.10.2026\nz\n")
    assert [s.day for s in doc.sections] == [None, None, None]


def test_diary_ingest_stores_iso_day_for_russian_headings(cfg, conn, data_dir):
    (data_dir / "дневник.md").write_text(
        "# Дневник\n\n## 16 октября 2026 — первый снег\nСнег.\n\n## 1 мая 2026 г.\nВесна.\n"
        "\n## 31.10.2026, 05:10 — взрослый японский референс\nНочь.\n",
        encoding="utf-8",
    )
    report = ingest(cfg, conn)
    assert not any(p["path"] == "дневник.md" for p in report.problems)
    rows = conn.execute(
        "SELECT s.day, s.title, s.anchor FROM sections s JOIN documents d ON d.id = s.doc_id "
        "WHERE d.kind = 'diary' ORDER BY s.position"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("2026-10-16", "первый снег", "2026-10-16"),
        ("2026-05-01", "", "2026-05-01"),
        ("2026-10-31", "взрослый японский референс", "2026-10-31"),
    ]


def test_markdown_escapes_html():
    html = render("<script>alert(1)</script> **жирный** [x](javascript:alert(1))")
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<strong>жирный</strong>" in html
    assert 'href="javascript' not in html


def test_extract_comfy_and_exif():
    comfy = (
        '{"3": {"class_type": "KSampler", "inputs": {"seed": 99}},'
        ' "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "m.safetensors"}},'
        ' "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "лиса"}}}'
    )
    m = extract_embedded({"prompt": comfy})
    assert (m.prompt, m.seed, m.model) == ("лиса", "99", "m.safetensors")
    m = extract_embedded({}, {"UserComment": "сова\nSteps: 5, Seed: 7, Model: owl"})
    assert (m.prompt, m.seed, m.model) == ("сова", "7", "owl")
    assert extract_embedded({"junk": 5}).prompt is None
