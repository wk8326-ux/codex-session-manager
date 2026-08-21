from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from runtime_migration import MigrationError, migrate_legacy_data
from runtime_paths import ApplicationPaths


class RuntimePathTests(unittest.TestCase):
    def test_source_mode_preserves_repository_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ApplicationPaths.resolve(root, [])

        self.assertEqual(paths.mode, "source")
        self.assertEqual(paths.data_root, root.resolve())
        self.assertEqual(paths.runtime_root, root.resolve() / ".runtime")
        self.assertEqual(paths.log_root, root.resolve() / "logs")
        self.assertEqual(
            paths.system_log_root,
            root.resolve() / ".runtime" / "system-startup",
        )

    def test_installed_mode_uses_local_app_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"LOCALAPPDATA": directory}, clear=False):
                paths = ApplicationPaths.resolve(Path(directory), ["--mode", "installed"])

        home = Path(directory).resolve() / "LocalProjectConsole"
        self.assertEqual(paths.data_root, home / "data")
        self.assertEqual(paths.runtime_root, home / "runtime")
        self.assertEqual(paths.log_root, home / "logs")
        self.assertEqual(paths.system_log_root, home / "logs" / "system-startup")

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

    def test_migration_copies_valid_data_and_stable_frp_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "projects.json").write_text(
                json.dumps([{"id": "project-a"}]), encoding="utf-8"
            )
            with closing(sqlite3.connect(legacy / "watchdog.db")) as database:
                database.execute("CREATE TABLE settings (value TEXT)")
                database.execute("INSERT INTO settings VALUES ('kept')")
                database.commit()
            frp = legacy / ".runtime" / "frp"
            frp.mkdir(parents=True)
            (frp / "frpc.toml").write_text("serverAddr='example'", encoding="utf-8")
            (frp / "frpc.log").write_text("discard", encoding="utf-8")

            paths = self._paths(root)
            result = migrate_legacy_data(legacy, paths)

            self.assertTrue(result.migrated)
            self.assertEqual(result.reason, "migrated")
            self.assertEqual(
                json.loads(paths.projects_path.read_text(encoding="utf-8")),
                [{"id": "project-a"}],
            )
            with closing(sqlite3.connect(paths.database_path)) as database:
                self.assertEqual(
                    database.execute("SELECT value FROM settings").fetchone()[0],
                    "kept",
                )
            self.assertTrue((paths.runtime_root / "frp" / "frpc.toml").exists())
            self.assertFalse((paths.runtime_root / "frp" / "frpc.log").exists())
            self.assertTrue((legacy / "projects.json").exists())

    def test_existing_destination_is_never_merged_or_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "projects.json").write_text("[]", encoding="utf-8")
            paths = self._paths(root)
            paths.ensure_writable_directories()
            paths.projects_path.write_text('[{"id":"existing"}]', encoding="utf-8")

            result = migrate_legacy_data(legacy, paths)

            self.assertFalse(result.migrated)
            self.assertEqual(result.reason, "destination_has_data")
            self.assertIn("existing", paths.projects_path.read_text(encoding="utf-8"))

    def test_source_without_legacy_data_reports_why_it_was_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            legacy.mkdir()
            paths = self._paths(root)

            result = migrate_legacy_data(legacy, paths)

            self.assertFalse(result.migrated)
            self.assertEqual(result.reason, "source_has_no_data")

    def test_explicit_replacement_backs_up_existing_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "projects.json").write_text(
                '[{"id":"legacy"}]', encoding="utf-8"
            )
            paths = self._paths(root)
            paths.ensure_writable_directories()
            paths.projects_path.write_text('[{"id":"current"}]', encoding="utf-8")

            result = migrate_legacy_data(legacy, paths, replace_existing=True)

            self.assertTrue(result.migrated)
            self.assertIsNotNone(result.backup)
            self.assertIn("legacy", paths.projects_path.read_text(encoding="utf-8"))
            backup_projects = result.backup / "projects.json"  # type: ignore[operator]
            self.assertIn("current", backup_projects.read_text(encoding="utf-8"))

    def test_replacement_failure_restores_existing_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "projects.json").write_text(
                '[{"id":"legacy"}]', encoding="utf-8"
            )
            paths = self._paths(root)
            paths.ensure_writable_directories()
            paths.projects_path.write_text('[{"id":"current"}]', encoding="utf-8")

            with patch(
                "runtime_migration._commit_directory",
                side_effect=OSError("simulated replacement failure"),
            ):
                with self.assertRaises(MigrationError):
                    migrate_legacy_data(legacy, paths, replace_existing=True)

            self.assertIn("current", paths.projects_path.read_text(encoding="utf-8"))
            self.assertFalse(any(root.glob("installed/data.pre-import*")))

    def test_invalid_projects_file_rolls_back_without_partial_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "projects.json").write_text("{}", encoding="utf-8")
            paths = self._paths(root)

            with self.assertRaises(MigrationError):
                migrate_legacy_data(legacy, paths)

            self.assertFalse(paths.projects_path.exists())
            self.assertFalse(paths.database_path.exists())

    def test_commit_failure_removes_every_partially_migrated_item(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "projects.json").write_text("[]", encoding="utf-8")
            with closing(sqlite3.connect(legacy / "watchdog.db")) as database:
                database.execute("CREATE TABLE settings (value TEXT)")
                database.commit()
            frp = legacy / ".runtime" / "frp"
            frp.mkdir(parents=True)
            (frp / "frpc.toml").write_text("serverAddr='example'", encoding="utf-8")
            paths = self._paths(root)

            from runtime_migration import _commit_directory

            def fail_on_frp(source: Path, destination: Path) -> None:
                if destination.name == "frp":
                    raise OSError("simulated FRP commit failure")
                _commit_directory(source, destination)

            with patch("runtime_migration._commit_directory", side_effect=fail_on_frp):
                with self.assertRaises(MigrationError):
                    migrate_legacy_data(legacy, paths)

            self.assertFalse(paths.projects_path.exists())
            self.assertFalse(paths.database_path.exists())
            self.assertFalse((paths.runtime_root / "frp").exists())
            self.assertTrue((legacy / "projects.json").exists())

    def test_successful_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "projects.json").write_text("[]", encoding="utf-8")
            paths = self._paths(root)

            first = migrate_legacy_data(legacy, paths)
            second = migrate_legacy_data(legacy, paths)

            self.assertTrue(first.migrated)
            self.assertFalse(second.migrated)
            self.assertEqual(paths.projects_path.read_text(encoding="utf-8"), "[]")


if __name__ == "__main__":
    unittest.main()
