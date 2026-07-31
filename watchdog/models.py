from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TurnSnapshot:
    id: str
    status: str
    error_message: str = ""
    error_kind: str = ""
    http_status: int | None = None


@dataclass(frozen=True)
class SessionSnapshot:
    thread_id: str
    name: str
    thread_status: str
    active_flags: tuple[str, ...]
    latest_turn: TurnSnapshot | None
