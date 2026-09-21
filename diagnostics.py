"""Minimal structured diagnostic reporting for recoverable failures.

Several subsystems deliberately continue after a secondary operation fails:
a missing audit row or an unreachable tunnel must not take the whole session
manager down. Swallowing those errors silently, however, made production
problems invisible, so every suppressed failure is reported here instead.

Output goes to stderr, which ``run_service`` redirects into the service log.
"""

from __future__ import annotations

import sys
import threading

from timeutil import utc_now


_lock = threading.Lock()


def report(component: str, message: str, error: BaseException | None = None) -> None:
    """Record a non-fatal failure without raising or printing a traceback."""

    detail = f"{type(error).__name__}: {error}" if error is not None else ""
    line = f"[{utc_now()}] {component}: {message}"
    if detail:
        line = f"{line} ({detail})"
    try:
        with _lock:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
    except (OSError, ValueError):
        # Diagnostics must never be the reason a request fails.
        pass
