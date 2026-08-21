from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from runtime_migration import MigrationError, migrate_legacy_data
from runtime_paths import ApplicationPaths


def create_database(path: Path, value: str) -> None:
    with closing(sqlite3.connect(path)) as database:
        database.execute("CREATE TABLE settings (value TEXT)")
        database.execute("INSERT INTO settings VALUES (?)", (value,))
        database.commit()


def read_value(path: Path) -> str:
    with closing(sqlite3.connect(path)) as database:
        return database.execute("SELECT value FROM settings").fetchone()[0]


class RuntimePathTests(unittest.TestCase):
    def test_source_mode_preserves_repository_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ApplicationPaths.resolve(root, [])

        self.assertEqual(paths.mode, "source")
        self.assertEqual(paths.data_root, root.resolve())
        self.assertEqual(paths.runtime_root, root.resolve() / ".runtime")
        self.assertEqual(paths.log_root, root.resolve() / "logs")

    def test_installed_mode_uses_session_manager_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"LOCALAPPDATA": directory}, clear=False):
                paths = ApplicationPaths.resolve(Path(directory), ["--mode", "installed"])

        home = Path(directory).resolve() / "CodexSessionManager"
        self.assertEqual(paths.data_root, home / "data")
        self.assertEqual(paths.runtime_root, home / "runtime")
        self.assertEqual(paths.log_root, home / "logs")

    def test_explicit_paths_override_mode_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ApplicationPaths.resolve(
                root,
                [
                    "--mode",
                    "portable",
                    "--data-dir",
                    str(root / "custom data"),
                    "--runtime-dir",
                    str(root / "custom runtime"),
                    "--log-dir",
                    str(root / "custom logs"),
                ],
            )

        self.assertEqual(paths.data_root, (root / "custom data").resolve())
        self.assertEqual(paths.runtime_root, (root / "custom runtime").resolve())
        self.assertEqual(paths.log_root, (root / "custom logs").resolve())


class RuntimeMigrationTests(unittest.TestCase):
    def _paths(self, root: Path) -> ApplicationPaths:
        return ApplicationPaths(
            resource_root=root,
            data_root=root / "installed" / "data",
            runtime_root=root / "installed" / "runtime",
            log_root=root / "installed" / "logs",
            mode="installed",
        )

    def test_migration_copies_database_and_stable_frp_files_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "projects.json").write_text('[{"id":"not-copied"}]', encoding="utf-8")
            create_database(legacy / "watchdog.db", "kept")
            frp = legacy / ".runtime" / "frp"
            frp.mkdir(parents=True)
            (frp / "frpc.toml").write_text("serverAddr='example'", encoding="utf-8")
            (frp / "frpc.log").write_text("discard", encoding="utf-8")
            (frp / "frpc.pid").write_text("123", encoding="ascii")

            paths = self._paths(root)
            result = migrate_legacy_data(legacy, paths)

            self.assertTrue(result.migrated)
            self.assertEqual(read_value(paths.database_path), "kept")
            self.assertFalse((paths.data_root / "projects.json").exists())
            self.assertTrue((paths.runtime_root / "frp" / "frpc.toml").exists())
            self.assertFalse((paths.runtime_root / "frp" / "frpc.log").exists())
            self.assertFalse((paths.runtime_root / "frp" / "frpc.pid").exists())
            self.assertTrue((legacy / "watchdog.db").exists())

    def test_existing_destination_is_not_overwritten_without_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            legacy.mkdir()
            create_database(legacy / "watchdog.db", "legacy")
            paths = self._paths(root)
            paths.ensure_writable_directories()
            create_database(paths.database_path, "current")

            result = migrate_legacy_data(legacy, paths)

            self.assertFalse(result.migrated)
            self.assertEqual(result.reason, "destination_has_data")
            self.assertEqual(read_value(paths.database_path), "current")

    def test_source_without_session_data_reports_why_it_was_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            legacy.mkdir()
            paths = self._paths(root)

            result = migrate_legacy_data(legacy, paths)

            self.assertFalse(result.migrated)
            self.assertEqual(result.reason, "source_has_no_session_data")

    def test_explicit_replacement_backs_up_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            legacy.mkdir()
            create_database(legacy / "watchdog.db", "legacy")
            paths = self._paths(root)
            paths.ensure_writable_directories()
            create_database(paths.database_path, "current")

            result = migrate_legacy_data(legacy, paths, replace_existing=True)

            self.assertTrue(result.migrated)
            self.assertIsNotNone(result.backup)
            self.assertEqual(read_value(paths.database_path), "legacy")
            self.assertEqual(read_value(result.backup / "watchdog.db"), "current")  # type: ignore[operator]

    def test_invalid_database_rolls_back_without_partial_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "watchdog.db").write_bytes(b"not a sqlite database")
            paths = self._paths(root)

            with self.assertRaises(MigrationError):
                migrate_legacy_data(legacy, paths)

            self.assertFalse(paths.database_path.exists())

    def test_successful_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            legacy.mkdir()
            create_database(legacy / "watchdog.db", "legacy")
            paths = self._paths(root)

            first = migrate_legacy_data(legacy, paths)
            second = migrate_legacy_data(legacy, paths)

            self.assertTrue(first.migrated)
            self.assertFalse(second.migrated)
            self.assertEqual(read_value(paths.database_path), "legacy")


if __name__ == "__main__":
    unittest.main()
