from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: Any


# Keep the public import stable while the complete dispatcher lives separately.
from .router import WatchdogRouter as WatchdogHttpApi  # noqa: E402

__all__ = ["ApiResponse", "WatchdogHttpApi"]
