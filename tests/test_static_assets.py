from __future__ import annotations

import gzip
import tempfile
import unittest
from pathlib import Path

from static_assets import StaticAssetCache


class StaticAssetCacheTests(unittest.TestCase):
    def test_compresses_text_when_client_accepts_gzip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.js"
            content = b"const value = 'cached';\n" * 100
            path.write_bytes(content)

            asset = StaticAssetCache().load(path, "br, gzip")

        self.assertEqual(asset.content_encoding, "gzip")
        self.assertEqual(gzip.decompress(asset.body), content)

    def test_respects_explicit_gzip_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.css"
            content = b".item { color: black; }\n" * 100
            path.write_bytes(content)

            asset = StaticAssetCache().load(path, "gzip;q=0, br")

        self.assertEqual(asset.content_encoding, "")
        self.assertEqual(asset.body, content)

    def test_invalidates_entry_when_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "page.html"
            path.write_bytes(b"a" * 2048)
            cache = StaticAssetCache()
            first = cache.load(path)
            path.write_bytes(b"b" * 3072)

            second = cache.load(path)

        self.assertNotEqual(first.body, second.body)
        self.assertEqual(second.body, b"b" * 3072)
