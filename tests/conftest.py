from __future__ import annotations

import hashlib
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin

from dom.config import Config
from dom.db import connect
from dom.ingest import ingest
from dom.web import create_app

CATALOGUE = """# Каталог артов

## 2025-03-14 — Кошка на крыше
`one.png` · PNG 64x48 · 1 KB · dreamy-xl, seed 12345

Рыжая кошка сидит на черепичной крыше под луной.
Вокруг тихо.

## 2025-03-15 — Утренний туман
`two.png` · PNG 32x32 · 900 B · aurora-v2, seed 777

Туман над рекой, едва видны лодки.

## 2025-03-16 — Потерянный
`missing.png` · PNG 10x10 · 1 KB · aurora-v2, seed 1

Файла нет на диске.
"""

DIARY = """# Дневник

Вступление к дневнику.

## 2025-03-14 — Первый день
Сегодня рисовала кошек и пила чай с **бергамотом**.

## 2025-03-15 — Туман
Утром был туман, и я гуляла у реки.
"""

STORY = """# Лесная сказка

Жили-были ёжики.

## Глава первая
Ёжик пошёл в лес за грибами.

## Глава вторая
Нашёл боровик.
"""

WARDROBE = """# Гардероб

## Платья
- синее
- зелёное
"""

A1111 = "кот на крыше, луна\nNegative prompt: blur\nSteps: 20, Seed: 12345, Model: dreamy-xl"


def make_png(path: Path, size: tuple[int, int], color: str, text: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    info = PngImagePlugin.PngInfo()
    for k, v in (text or {}).items():
        info.add_itxt(k, v)
    Image.new("RGB", size, color).save(path, "PNG", pnginfo=info)


def make_wav(path: Path, seconds: float = 0.5, rate: int = 8000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "Shinobu"
    make_png(d / "arts" / "2025-03-14" / "one.png", (64, 48), "orange", {"parameters": A1111})
    make_png(d / "арты" / "2025-03-15" / "two.png", (32, 32), "gray")
    make_png(d / "арты" / "three.png", (16, 16), "blue")
    (d / "арты" / "каталог.md").write_text(CATALOGUE, encoding="utf-8")
    two_sha = hashlib.sha256((d / "арты" / "2025-03-15" / "two.png").read_bytes()).hexdigest()
    (d / "арты" / "контрольные-суммы.txt").write_text(
        f"{two_sha}  2025-03-15/two.png\n{'0' * 64}  three.png\nэто не строка суммы\n",
        encoding="utf-8",
    )
    (d / "дневник.md").write_text(DIARY, encoding="utf-8")
    (d / "рассказы").mkdir()
    (d / "рассказы" / "2025-03-15-лес.md").write_text(STORY, encoding="utf-8")
    make_wav(d / "голосовые" / "2025-03-14-утро.wav")
    (d / "гардероб.md").write_text(WARDROBE, encoding="utf-8")
    return d


@pytest.fixture
def cfg(tmp_path: Path, data_dir: Path) -> Config:
    return Config(
        data_dir=data_dir, db_path=tmp_path / "state" / "dom.sqlite3", cache_dir=tmp_path / "cache"
    )


@pytest.fixture
def conn(cfg: Config):
    c = connect(cfg.db_path)
    yield c
    c.close()


@pytest.fixture
def ingested(cfg: Config, conn):
    return ingest(cfg, conn)


@pytest.fixture
def client(cfg: Config, ingested):
    with TestClient(create_app(cfg)) as c:
        yield c
