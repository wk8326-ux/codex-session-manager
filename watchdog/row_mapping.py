from __future__ import annotations

import sqlite3


ALIASES = {
    "base_url": "baseUrl", "probe_url_override": "probeUrlOverride",
    "api_key_ciphertext": "encryptedKey", "timeout_seconds": "timeoutSeconds",
    "last_probe_status": "lastProbeStatus", "last_http_status": "lastHttpStatus",
    "last_probe_detail": "lastProbeDetail", "last_checked_at": "lastCheckedAt",
    "created_at": "createdAt", "updated_at": "updatedAt", "thread_id": "threadId",
    "host_kind": "hostKind", "channel_id": "channelId",
    "interval_minutes": "intervalMinutes", "resume_prompt": "resumePrompt",
    "unattended_approvals_enabled": "unattendedApprovalsEnabled",
    "last_session_state": "lastSessionState", "last_turn_id": "lastTurnId",
    "last_check_result": "lastCheckResult", "next_check_at": "nextCheckAt",
    "match_type": "matchType", "first_seen_at": "firstSeenAt",
    "error_signature": "errorSignature", "attempt_count": "attemptCount",
    "last_attempt_at": "lastAttemptAt", "resolved_at": "resolvedAt",
    "session_id": "sessionId", "started_at": "startedAt", "finished_at": "finishedAt",
    "channel_status": "channelStatus", "http_status": "httpStatus",
    "session_state": "sessionState", "turn_id": "turnId",
    "error_category": "errorCategory", "resumed_turn_id": "resumedTurnId",
    "resume_attempt": "resumeAttempt", "duration_ms": "durationMs",
    "detail_sanitized": "detailSanitized",
    "default_interval_minutes": "defaultIntervalMinutes",
    "minimum_interval_minutes": "minimumIntervalMinutes",
    "record_retention_days": "recordRetentionDays", "record_limit": "recordLimit",
    "scheduler_enabled": "schedulerEnabled",
    "resume_actions_enabled": "resumeActionsEnabled",
    "resume_dispatch_mode": "resumeDispatchMode", "is_builtin": "builtIn",
    "incident_fingerprint": "incidentFingerprint", "incident_attempt": "incidentAttempt",
    "monitor_run_id": "monitorRunId", "runner_id": "runnerId",
    "lease_token": "leaseToken", "lease_expires_at": "leaseExpiresAt",
    "claim_attempt_count": "claimAttemptCount", "claimed_at": "claimedAt",
}

BOOLEAN_FIELDS = {
    "enabled", "schedulerEnabled", "resumeActionsEnabled",
    "unattendedApprovalsEnabled", "builtIn",
}


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    values = dict(row)
    for source, target in ALIASES.items():
        if source in values:
            values[target] = values.pop(source)
    for key in BOOLEAN_FIELDS:
        if key in values:
            values[key] = bool(values[key])
    return values
