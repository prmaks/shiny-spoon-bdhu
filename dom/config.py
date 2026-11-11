"""Configuration from environment variables only."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    pass


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class Config:
    data_dir: Path
    db_path: Path
    cache_dir: Path
    host: str = "127.0.0.1"
    port: int = 8000
    pick_cooldown_days: int = 5
    captions_path: Path | None = None  # default: <state dir>/captions.jsonl
    voices_path: Path | None = None  # default: <state dir>/voices.jsonl

    @property
    def state_dir(self) -> Path:
        return self.db_path.parent

    @property
    def captions_file(self) -> Path:
        return self.captions_path or self.state_dir / "captions.jsonl"

    @property
    def voices_file(self) -> Path:
        return self.voices_path or self.state_dir / "voices.jsonl"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ if env is None else env)
        data_dir = Path(env.get("DOM_DATA_DIR") or "~/Shinobu").expanduser()
        db_path = Path(env.get("DOM_DB_PATH") or "./dom.sqlite3").expanduser()
        cache_dir = Path(env.get("DOM_CACHE_DIR") or "./dom-cache").expanduser()
        try:
            port = int(env.get("DOM_PORT") or 8000)
            cooldown = int(env.get("DOM_PICK_COOLDOWN_DAYS") or 5)
        except ValueError as exc:
            raise ConfigError(f"Некорректное числовое значение в окружении: {exc}") from exc
        captions = env.get("DOM_CAPTIONS")
        voices = env.get("DOM_VOICES")
        cfg = cls(
            data_dir=data_dir,
            db_path=db_path,
            cache_dir=cache_dir,
            host=env.get("DOM_HOST") or "127.0.0.1",
            port=port,
            pick_cooldown_days=max(0, cooldown),
            captions_path=Path(captions).expanduser() if captions else None,
            voices_path=Path(voices).expanduser() if voices else None,
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        # The data dir is strictly read-only: refuse configs that would write into it.
        if _is_inside(self.db_path, self.data_dir):
            raise ConfigError("DOM_DB_PATH не может находиться внутри DOM_DATA_DIR")
        if _is_inside(self.cache_dir, self.data_dir):
            raise ConfigError("DOM_CACHE_DIR не может находиться внутри DOM_DATA_DIR")
