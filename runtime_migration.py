"""Transactional migration of legacy source data into an application home."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from runtime_paths import ApplicationPaths


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationResult:
    migrated: bool
    source: Path
    destination: Path
    copied: tuple[str, ...]
    reason: str
    backup: Path | None = None


def _validate_projects(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise MigrationError("projects.json must contain a JSON array")


def _copy_database(source: Path, destination: Path) -> None:
    with closing(sqlite3.connect(source)) as source_db, closing(
        sqlite3.connect(destination)
    ) as target_db:
        source_db.backup(target_db)
        result = target_db.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise MigrationError("watchdog.db failed its integrity check")


def _stage_frp_runtime(source_root: Path, staging_root: Path) -> bool:
    source = source_root / ".runtime" / "frp"
    if not source.is_dir():
        return False
    destination = staging_root / "frp"
    destination.mkdir(parents=True, exist_ok=True)
    copied = False
    for item in source.iterdir():
        if not item.is_file() or item.suffix.lower() in {".log", ".pid", ".lock"}:
            continue
        shutil.copy2(item, destination / item.name)
        copied = True
    return copied


def _commit_directory(source: Path, destination: Path) -> None:
    source.replace(destination)


def _remove_empty_directory(path: Path) -> bool:
    try:
        path.rmdir()
        return True
    except OSError:
        return False


def _backup_path(data_root: Path) -> Path:
    candidate = data_root.with_name(f"{data_root.name}.pre-import")
    index = 1
    while candidate.exists():
        candidate = data_root.with_name(f"{data_root.name}.pre-import-{index}")
        index += 1
    return candidate


def _rollback(
    committed: list[Path], data_root: Path, backup: Path | None
) -> None:
    for destination in reversed(committed):
        if destination.is_dir():
            shutil.rmtree(destination, ignore_errors=True)
        else:
            destination.unlink(missing_ok=True)
    if backup is not None and backup.exists():
        backup.replace(data_root)
    else:
        data_root.mkdir(parents=True, exist_ok=True)


def migrate_legacy_data(
    source_root: Path,
    paths: ApplicationPaths,
    *,
    replace_existing: bool = False,
) -> MigrationResult:
    """Copy legacy data once, leaving the source untouched on success or failure."""

    source_root = Path(source_root).resolve()
    paths.ensure_writable_directories()
    if not replace_existing and (
        paths.projects_path.exists() or paths.database_path.exists()
    ):
        return MigrationResult(
            False, source_root, paths.data_root, (), "destination_has_data"
        )

    projects_source = source_root / "projects.json"
    database_source = source_root / "watchdog.db"
    if not projects_source.exists() and not database_source.exists():
        return MigrationResult(False, source_root, paths.data_root, (), "source_has_no_data")

    copied: list[str] = []
    staging_parent = paths.data_root.parent
    staging = Path(tempfile.mkdtemp(prefix="lpc-migration-", dir=staging_parent))
    staged_data = staging / "data"
    staged_runtime = staging / "runtime"
    staged_data.mkdir()
    committed: list[Path] = []
    backup: Path | None = None
    try:
        if projects_source.is_file():
            staged_projects = staged_data / "projects.json"
            shutil.copy2(projects_source, staged_projects)
            _validate_projects(staged_projects)
            copied.append("projects.json")
        if database_source.is_file():
            _copy_database(database_source, staged_data / "watchdog.db")
            copied.append("watchdog.db")

        staged_frp = _stage_frp_runtime(source_root, staged_runtime)
        if paths.data_root.exists():
            if replace_existing and any(paths.data_root.iterdir()):
                backup = _backup_path(paths.data_root)
                paths.data_root.replace(backup)
            else:
                _remove_empty_directory(paths.data_root)
            if paths.data_root.exists():
                raise MigrationError(
                    f"destination data directory is not empty: {paths.data_root}"
                )

        _commit_directory(staged_data, paths.data_root)
        committed.append(paths.data_root)

        frp_destination = paths.runtime_root / "frp"
        if staged_frp and not frp_destination.exists():
            _commit_directory(staged_runtime / "frp", frp_destination)
            committed.append(frp_destination)
            copied.append("runtime/frp")
    except MigrationError:
        _rollback(committed, paths.data_root, backup)
        raise
    except (OSError, sqlite3.DatabaseError, json.JSONDecodeError) as error:
        _rollback(committed, paths.data_root, backup)
        raise MigrationError(str(error)) from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return MigrationResult(
        True, source_root, paths.data_root, tuple(copied), "migrated", backup
    )
