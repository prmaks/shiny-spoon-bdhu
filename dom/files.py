"""Read-only access to files in the data dir for stories: safe lookup and name resolution."""

from __future__ import annotations

import os
import threading
import time
import unicodedata
from pathlib import Path, PurePosixPath

from .ingest import excluded_reason
from .markup import safe_relpath

MAX_BYTES = 20 * 1024 * 1024
PAGE_TYPES = {
    "html": "text/html; charset=utf-8",
    "htm": "text/html; charset=utf-8",
    "svg": "image/svg+xml",
    "css": "text/css; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
}
MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "ogg": "audio/ogg",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
}
ALL_TYPES = {**PAGE_TYPES, **MEDIA_TYPES}
SEARCH_DEPTH = 3
INDEX_TTL = 30.0


class Rejected(LookupError):
    def __init__(self, status: int = 404) -> None:
        super().__init__(status)
        self.status = status


def data_file(root: Path, relpath: str, types: dict[str, str]) -> tuple[Path, str]:
    """Resolve ``relpath`` inside ``root`` to (file, content type) or raise Rejected.

    Rejects traversal, dotfiles, symlinks leading outside, unknown types, files over
    MAX_BYTES and art intermediates the library hides on purpose.
    """
    clean = safe_relpath(relpath)
    ctype = types.get(PurePosixPath(relpath).suffix.lower().lstrip("."))
    if clean is None or ctype is None:
        raise Rejected()
    root = root.resolve()
    full = (root / clean).resolve()
    if not full.is_relative_to(root) or not full.is_file() or excluded_reason(full):
        raise Rejected()
    if full.stat().st_size > MAX_BYTES:
        raise Rejected(413)
    return full, ctype


def _key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


class Resolver:
    """Maps story references to data-dir paths: as written (next to the story, then from
    the root), else the first file with the same name within SEARCH_DEPTH levels."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._index: dict[str, str] = {}
        self._built = -INDEX_TTL
        self._lock = threading.Lock()

    def _by_name(self) -> dict[str, str]:
        with self._lock:
            if time.monotonic() - self._built > INDEX_TTL:
                index: dict[str, str] = {}
                root = str(self.root)
                for dirpath, dirnames, filenames in os.walk(root):
                    depth = os.path.relpath(dirpath, root).count(os.sep) + (dirpath != root)
                    dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                    if depth >= SEARCH_DEPTH - 1:
                        dirnames.clear()
                    for name in sorted(filenames):
                        if not name.startswith("."):
                            rel = Path(dirpath, name).relative_to(self.root).as_posix()
                            index.setdefault(_key(name), rel)
                self._index, self._built = index, time.monotonic()
            return self._index

    def __call__(self, token: str, base: str = "") -> str | None:
        clean = safe_relpath(token)
        if clean is None:
            return None
        for cand in (f"{base}/{clean}" if base else None, clean):
            if cand:
                try:
                    data_file(self.root, cand, ALL_TYPES)
                    return cand
                except Rejected:
                    pass
        found = self._by_name().get(_key(PurePosixPath(clean).name))
        if found is None:
            return None
        try:
            data_file(self.root, found, ALL_TYPES)
        except Rejected:
            return None
        return found
