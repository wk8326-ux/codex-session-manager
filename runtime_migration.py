"""Transactional import of legacy session data from Local Project Console."""

from __future__ import annotations

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


def _first_file(candidates: tuple[Path, ...]) -> Path | None:
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _first_directory(candidates: tuple[Path, ...]) -> Path | None:
    return next((candidate for candidate in candidates if candidate.is_dir()), None)


def _copy_database(source: Path, destination: Path) -> None:
    with (
        closing(sqlite3.connect(source)) as source_db,
        closing(sqlite3.connect(destination)) as target_db,
    ):
        source_db.backup(target_db)
        result = target_db.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise MigrationError("watchdog.db failed its integrity check")


def _stage_frp_runtime(source_root: Path, staging_root: Path) -> bool:
    runtime_root = _first_directory((source_root / ".runtime", source_root / "runtime"))
    source = runtime_root / "frp" if runtime_root is not None else None
    if source is None or not source.is_dir():
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


def _backup_path(data_root: Path) -> Path:
    candidate = data_root.parent / f"{data_root.name}.pre-session-import"
    index = 1
    while candidate.exists():
        candidate = data_root.parent / f"{data_root.name}.pre-session-import-{index}"
        index += 1
    return candidate


def migrate_legacy_data(
    source_root: Path,
    paths: ApplicationPaths,
    *,
    replace_existing: bool = False,
) -> MigrationResult:
    """Import the shared database and stable tunnel files without touching source."""

    source_root = Path(source_root).resolve()
    paths.ensure_writable_directories()
    if paths.database_path.exists() and not replace_existing:
        return MigrationResult(
            False, source_root, paths.data_root, (), "destination_has_data"
        )

    database_source = _first_file(
        (source_root / "watchdog.db", source_root / "data" / "watchdog.db")
    )
    runtime_source = _first_directory(
        (source_root / ".runtime" / "frp", source_root / "runtime" / "frp")
    )
    if database_source is None and runtime_source is None:
        return MigrationResult(
            False, source_root, paths.data_root, (), "source_has_no_session_data"
        )

    copied: list[str] = []
    staging = Path(
        tempfile.mkdtemp(prefix="csm-migration-", dir=paths.data_root.parent)
    )
    staged_database = staging / "watchdog.db"
    staged_runtime = staging / "runtime"
    backup: Path | None = None
    database_committed = False
    frp_committed = False
    try:
        if database_source is not None:
            _copy_database(database_source, staged_database)
        staged_frp = _stage_frp_runtime(source_root, staged_runtime)

        if database_source is not None:
            if paths.database_path.exists():
                backup = _backup_path(paths.data_root)
                backup.mkdir(parents=True)
                shutil.copy2(paths.database_path, backup / "watchdog.db")
            staged_database.replace(paths.database_path)
            database_committed = True
            copied.append("watchdog.db")

        frp_destination = paths.runtime_root / "frp"
        if staged_frp and not frp_destination.exists():
            (staged_runtime / "frp").replace(frp_destination)
            frp_committed = True
            copied.append("runtime/frp")
    except MigrationError:
        raise
    except (OSError, sqlite3.DatabaseError) as error:
        if frp_committed:
            shutil.rmtree(paths.runtime_root / "frp", ignore_errors=True)
        if database_committed:
            if backup is not None:
                shutil.copy2(backup / "watchdog.db", paths.database_path)
            else:
                paths.database_path.unlink(missing_ok=True)
        if backup is not None and backup.exists() and not any(backup.iterdir()):
            backup.rmdir()
        raise MigrationError(str(error)) from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return MigrationResult(
        True, source_root, paths.data_root, tuple(copied), "migrated", backup
    )
