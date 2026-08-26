from __future__ import annotations

import gzip
import threading
from dataclasses import dataclass
from pathlib import Path


_COMPRESSIBLE_SUFFIXES = {".css", ".html", ".js", ".json", ".webmanifest"}


@dataclass(frozen=True)
class StaticAsset:
    body: bytes
    content_encoding: str


class StaticAssetCache:
    """Caches immutable resource bytes and their compressed representation."""

    def __init__(self, *, minimum_compress_size: int = 1024) -> None:
        self._minimum_compress_size = minimum_compress_size
        self._lock = threading.RLock()
        self._entries: dict[Path, tuple[tuple[int, int], bytes, bytes | None]] = {}

    def load(self, path: Path, accept_encoding: str = "") -> StaticAsset:
        resolved = Path(path).resolve()
        stat = resolved.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            cached = self._entries.get(resolved)
            if cached is None or cached[0] != signature:
                raw = resolved.read_bytes()
                compressed = (
                    gzip.compress(raw, compresslevel=6)
                    if len(raw) >= self._minimum_compress_size
                    and resolved.suffix.lower() in _COMPRESSIBLE_SUFFIXES
                    else None
                )
                cached = (signature, raw, compressed)
                self._entries[resolved] = cached
        if cached[2] is not None and _accepts_gzip(accept_encoding):
            return StaticAsset(cached[2], "gzip")
        return StaticAsset(cached[1], "")


def _accepts_gzip(value: str) -> bool:
    for entry in value.lower().split(","):
        parts = [part.strip() for part in entry.split(";")]
        if parts[0] not in {"gzip", "*"}:
            continue
        quality = next((part for part in parts[1:] if part.startswith("q=")), "q=1")
        try:
            return float(quality[2:]) > 0
        except ValueError:
            return False
    return False
