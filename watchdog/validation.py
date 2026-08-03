from __future__ import annotations

from urllib.parse import urlparse
from uuid import UUID


class ValidationError(ValueError):
    """Raised when a user-provided watchdog value is invalid."""


def validate_http_url(value: object) -> str:
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValidationError("channel URL must be a valid HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValidationError("channel URL must not contain credentials")
    return raw.rstrip("/")


def validate_thread_id(value: object) -> str:
    raw = str(value or "").strip()
    try:
        UUID(raw)
    except (ValueError, AttributeError):
        raise ValidationError("thread ID must be a UUID") from None
    return raw


def validate_interval(value: object, *, minimum: int = 5) -> int:
    try:
        interval = int(value)
    except (TypeError, ValueError):
        raise ValidationError("interval must be an integer") from None
    if interval < minimum:
        raise ValidationError(f"interval must be at least {minimum} minutes")
    return interval


def validate_resume_prompt(value: object) -> str:
    prompt = str(value or "").strip()
    if not prompt:
        raise ValidationError("resume prompt must not be empty")
    if len(prompt) > 4000:
        raise ValidationError("resume prompt must be at most 4000 characters")
    return prompt


def validate_required_text(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValidationError(f"{field} must not be empty")
    return text


def validate_recovery_rule_name(value: object) -> str:
    name = validate_required_text(value, "name")
    if len(name) > 120:
        raise ValidationError("name must be at most 120 characters")
    return name


def validate_recovery_rule_pattern(value: object) -> str:
    pattern = str(value or "").strip()
    if len(pattern) < 8:
        raise ValidationError("error text must be at least 8 characters")
    if len(pattern) > 500:
        raise ValidationError("error text must be at most 500 characters")
    return pattern
