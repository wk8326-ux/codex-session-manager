"""Local Codex session watchdog support.

Public names are resolved lazily so importing one watchdog submodule does not
pull the HTTP router and the complete application graph into the process.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import SessionSnapshot, TurnSnapshot
    from .router import ApiResponse, WatchdogHttpApi

__all__ = ["ApiResponse", "SessionSnapshot", "TurnSnapshot", "WatchdogHttpApi"]


def __getattr__(name: str):
    if name in {"ApiResponse", "WatchdogHttpApi"}:
        from .router import ApiResponse, WatchdogHttpApi

        return {"ApiResponse": ApiResponse, "WatchdogHttpApi": WatchdogHttpApi}[name]
    if name in {"SessionSnapshot", "TurnSnapshot"}:
        from .models import SessionSnapshot, TurnSnapshot

        return {"SessionSnapshot": SessionSnapshot, "TurnSnapshot": TurnSnapshot}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
