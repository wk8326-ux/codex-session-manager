"""Resolve immutable application resources and writable runtime locations."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


APP_NAME = "CodexSessionManager"
APP_VERSION = "0.1.0"


def _argument_value(arguments: Sequence[str], name: str) -> str | None:
    try:
        index = arguments.index(name)
    except ValueError:
        return None
    if index + 1 >= len(arguments):
        return None
    return arguments[index + 1]


def _resource_root(source_root: Path) -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    return Path(frozen_root).resolve() if frozen_root else source_root.resolve()


def _installed_home() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        local_app_data = str(Path.home() / "AppData" / "Local")
    return Path(local_app_data).expanduser().resolve() / APP_NAME


@dataclass(frozen=True)
class ApplicationPaths:
    resource_root: Path
    data_root: Path
    runtime_root: Path
    log_root: Path
    mode: str

    def ensure_writable_directories(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.log_root.mkdir(parents=True, exist_ok=True)

    @property
    def database_path(self) -> Path:
        return self.data_root / "watchdog.db"

    @property
    def system_runtime_root(self) -> Path:
        return self.runtime_root / "system-startup"

    @property
    def system_log_root(self) -> Path:
        # Keep source-mode diagnostics where existing scripts and users expect them.
        if self.mode == "source":
            return self.system_runtime_root
        return self.log_root / "system-startup"

    @classmethod
    def resolve(
        cls,
        source_root: Path,
        arguments: Sequence[str] | None = None,
    ) -> "ApplicationPaths":
        arguments = list(sys.argv[1:] if arguments is None else arguments)
        source_root = Path(source_root).resolve()
        resource_root = _resource_root(source_root)
        frozen = bool(getattr(sys, "frozen", False))
        requested_mode = (
            (
                _argument_value(arguments, "--mode")
                or os.environ.get("CSM_RUNTIME_MODE")
                or ("installed" if frozen else "source")
            )
            .strip()
            .lower()
        )
        if requested_mode not in {"source", "installed", "portable"}:
            requested_mode = "installed" if frozen else "source"

        explicit_data = _argument_value(arguments, "--data-dir") or os.environ.get(
            "CSM_DATA_DIR"
        )
        if explicit_data:
            data_root = Path(explicit_data).expanduser().resolve()
            home_root = data_root.parent
        elif requested_mode == "source":
            data_root = source_root
            home_root = source_root
        elif requested_mode == "portable":
            executable_root = Path(sys.executable).resolve().parent
            home_root = executable_root
            data_root = home_root / "data"
        else:
            home_root = _installed_home()
            data_root = home_root / "data"

        explicit_runtime = _argument_value(
            arguments, "--runtime-dir"
        ) or os.environ.get("CSM_RUNTIME_DIR")
        explicit_logs = _argument_value(arguments, "--log-dir") or os.environ.get(
            "CSM_LOG_DIR"
        )
        runtime_root = (
            Path(explicit_runtime).expanduser().resolve()
            if explicit_runtime
            else (
                data_root / ".runtime"
                if requested_mode == "source"
                else home_root / "runtime"
            )
        )
        log_root = (
            Path(explicit_logs).expanduser().resolve()
            if explicit_logs
            else (
                data_root / "logs" if requested_mode == "source" else home_root / "logs"
            )
        )
        return cls(resource_root, data_root, runtime_root, log_root, requested_mode)
