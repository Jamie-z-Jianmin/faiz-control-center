"""Typed data contracts and secret redaction for Faiz Control Center."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Optional


_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "sk-***"),
    (re.compile(r"\bAIzaSy[A-Za-z0-9_-]{8,}"), "AIzaSy***"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{6,}"), "Bearer ***"),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]{8,}"), "ya29.***"),
)


def redact_secrets(text: str) -> str:
    """Redact API keys, Bearer tokens, and OAuth tokens from arbitrary text."""
    if not text:
        return text
    result = text
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def sanitize_for_json(value: Any) -> Any:
    """Recursively convert dataclasses/tuples and redact string fields."""
    if hasattr(value, "__dataclass_fields__"):
        return sanitize_for_json(asdict(value))
    if isinstance(value, dict):
        return {str(k): sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_for_json(v) for v in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value


@dataclass(frozen=True)
class ModelBucketQuota:
    """Quota limits and refresh windows for a model group."""

    group_name: str
    models: tuple[str, ...] = ()
    weekly_pct: Optional[float] = None
    weekly_refresh: str = ""
    five_hour_pct: Optional[float] = None
    five_hour_refresh: str = ""


@dataclass(frozen=True)
class ProcessChannelInfo:
    """Live local agy process channel split (Pro vs GCP ADC)."""

    pro_count: int = 0
    gcp_count: int = 0
    quota_project: str = ""
    active_auth_method: str = ""


@dataclass(frozen=True)
class TokenBurnStats:
    """Incremental transcript token burn and prompt cache hit statistics."""

    tokens_1h: int = 0
    tokens_24h: int = 0
    cache_hit_rate: float = 0.0
    active_sessions: int = 0
    by_model_24h: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class QuotaSnapshot:
    """Aggregated quota, channel, and burn-rate snapshot."""

    fetched_at: str
    account: str = ""
    buckets: tuple[ModelBucketQuota, ...] = ()
    channels: ProcessChannelInfo = field(default_factory=ProcessChannelInfo)
    burn: TokenBurnStats = field(default_factory=TokenBurnStats)
    quota_gate: str = "GREEN"
    source: str = "init"
    pty_fetched_epoch: int = 0
    error: Optional[str] = None
    stale: bool = False


@dataclass(frozen=True)
class GceInstanceInfo:
    """Metadata for a Google Compute Engine instance."""

    name: str
    zone: str
    status: str
    machine_type: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class ColabGcpSnapshot:
    """Colab boot.sh status, GCE Spot instances, and Google Drive quota snapshot."""

    fetched_at: str
    colab_state: str = "UNKNOWN"
    raw_status: str = ""
    project: str = ""
    mode: str = ""
    reason: str = ""
    updated_at: str = ""
    age_seconds: Optional[int] = None
    idle_remaining_seconds: Optional[int] = None
    webui_url: str = ""
    gce_instances: tuple[GceInstanceInfo, ...] = ()
    drive_used_bytes: Optional[int] = None
    drive_total_bytes: Optional[int] = None
    drive_free_bytes: Optional[int] = None
    rclone_readonly: bool = False
    rclone_shared_client_id: bool = False
    error: Optional[str] = None
    stale: bool = False


@dataclass(frozen=True)
class MailboxItem:
    """A single file/job entry in Colab_Notebooks/mailbox."""

    name: str
    queue: str
    size: int = 0
    modified_at: str = ""
    is_tts: bool = False
    verified: Optional[bool] = None
    verify_detail: str = ""
    log_tail: str = ""


@dataclass(frozen=True)
class MailboxSnapshot:
    """Queue depths, failure previews, and anti-false-done verification results."""

    fetched_at: str
    tasks_count: int = 0
    done_count: int = 0
    failed_count: int = 0
    tasks: tuple[MailboxItem, ...] = ()
    done_items: tuple[MailboxItem, ...] = ()
    failed_items: tuple[MailboxItem, ...] = ()
    error: Optional[str] = None
    stale: bool = False
