"""Colab CLI control-plane, boot.sh status.json, GCE Spot instances, and Drive quota collector."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import time
from typing import Any, Optional

from ..gateways.colab import ColabCliGateway
from ..gateways.drive import DriveGateway
from ..gateways.exec_util import resolve_binary, run_exec
from ..models import ColabGcpSnapshot, GceInstanceInfo
from ..statusline_config import get_cloud_settings
from ..store import atomic_write_json

DEFAULT_GCLOUD_BIN = resolve_binary(
    "gcloud",
    Path.home() / ".local" / "bin" / "gcloud",
    "/opt/homebrew/bin/gcloud",
    "/usr/local/bin/gcloud",
)
IDLE_LIMIT_SECONDS = 3600
STALE_THRESHOLD_SECONDS = 180
BACKOFF_STEPS = (60.0, 120.0, 300.0)
RATE_LIMIT_BACKOFF_STEPS = (180.0, 360.0, 600.0)

_TZ_OFFSETS = {
    "UTC": timezone.utc,
    "GMT": timezone.utc,
    "CST": timezone(timedelta(hours=8)),
    "PDT": timezone(timedelta(hours=-7)),
    "PST": timezone(timedelta(hours=-8)),
    "EDT": timezone(timedelta(hours=-4)),
    "EST": timezone(timedelta(hours=-5)),
}

_GNU_DATE_RE = re.compile(
    r"^(?:[A-Za-z]{3}\s+)?([A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}(?:\s+[AP]M)?)\s+([A-Z]{2,5})\s+(\d{4})$"
)


def parse_colab_timestamp(ts_str: str) -> Optional[datetime]:
    """Parse ISO-8601 or GNU `date` strings (e.g. 'Wed Nov 18 09:06:22 AM UTC 2026') to UTC."""
    if not ts_str or not isinstance(ts_str, str):
        return None
    raw = " ".join(ts_str.strip().split())

    # 1. Try ISO-8601 first
    try:
        iso_candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass

    # 2. Try GNU `date` with optional weekday & AM/PM + TZ abbreviation
    m = _GNU_DATE_RE.match(raw)
    if m:
        core_dt, tz_abbr, year_str = m.group(1), m.group(2), m.group(3)
        tz = _TZ_OFFSETS.get(tz_abbr.upper())
        if tz is not None:
            for fmt in ("%b %d %I:%M:%S %p %Y", "%b %d %H:%M:%S %Y"):
                try:
                    dt = datetime.strptime(f"{core_dt} {year_str}", fmt)
                    return dt.replace(tzinfo=tz).astimezone(timezone.utc)
                except ValueError:
                    continue
    return None


class CloudProbe:
    """Collects Colab control-plane sessions, Drive status.json, GCE Spot, and Drive quota."""

    def __init__(
        self,
        drive: DriveGateway,
        *,
        colab_gateway: Optional[ColabCliGateway] = None,
        gcloud_bin: str = DEFAULT_GCLOUD_BIN,
        gcp_project: Optional[str] = None,
        colab_status_path: Optional[str] = None,
        semaphore: Optional[asyncio.Semaphore] = None,
        about_ttl_sec: float = 300.0,
        backoff_path: Optional[Path] = None,
    ) -> None:
        cloud_cfg = get_cloud_settings()
        self.drive = drive
        self.colab_gateway = colab_gateway
        self.gcloud_bin = gcloud_bin
        self.gcp_project = gcp_project or cloud_cfg["gcp_project"]
        self.colab_status_path = colab_status_path or cloud_cfg["colab_status_path"]
        self.semaphore = semaphore
        self.about_ttl_sec = about_ttl_sec
        self.backoff_path = backoff_path

        self._last_snapshot: Optional[ColabGcpSnapshot] = None
        self._last_about: dict[str, Optional[int]] = {"used": None, "total": None, "free": None}
        self._last_about_ts: float = 0.0
        self._error_streak: int = 0
        self._next_allowed_epoch: float = 0.0
        self._last_error_kind: str = ""
        self._last_error: str = ""
        self._in_backoff_skip: bool = False
        self._load_backoff()

    @property
    def in_backoff_skip(self) -> bool:
        """Whether the most recent collect() skipped Drive calls or hit Drive rate-limit."""
        return self._in_backoff_skip

    def _load_backoff(self) -> None:
        if self.backoff_path is None or not self.backoff_path.exists():
            return
        try:
            data = json.loads(self.backoff_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        try:
            self._error_streak = max(0, int(data.get("error_streak") or 0))
            self._next_allowed_epoch = float(data.get("next_allowed_epoch") or 0.0)
            self._last_error_kind = str(data.get("last_error_kind") or "")
            self._last_error = str(data.get("last_error") or "")
        except (TypeError, ValueError):
            self._error_streak = 0
            self._next_allowed_epoch = 0.0

    def _save_backoff(self) -> None:
        if self.backoff_path is None:
            return
        try:
            atomic_write_json(
                self.backoff_path,
                {
                    "error_streak": self._error_streak,
                    "next_allowed_epoch": self._next_allowed_epoch,
                    "last_error_kind": self._last_error_kind,
                    "last_error": self._last_error,
                    "updated_epoch": int(time.time()),
                },
            )
        except Exception:
            pass

    @staticmethod
    def _classify_error_kind(err_text: str) -> str:
        upper = err_text.upper()
        if "RATE_LIMIT" in upper or "RATELIMIT" in upper or "403" in upper:
            return "rate_limit"
        if "TIMED OUT" in upper or "TIMEOUT" in upper:
            return "timeout"
        return "error" if err_text else ""

    def next_backoff_seconds(self) -> float:
        steps = RATE_LIMIT_BACKOFF_STEPS if self._last_error_kind == "rate_limit" else BACKOFF_STEPS
        idx = min(max(0, self._error_streak - 1), len(steps) - 1)
        return steps[idx]

    def _record_drive_failure(self, err_msg: str, now_ep: float) -> None:
        self._error_streak += 1
        self._last_error_kind = self._classify_error_kind(err_msg)
        self._last_error = err_msg
        self._next_allowed_epoch = now_ep + self.next_backoff_seconds()
        self._save_backoff()
        if self._last_error_kind == "rate_limit":
            self._in_backoff_skip = True

    def _reset_drive_backoff(self) -> None:
        if self._error_streak != 0 or self._next_allowed_epoch != 0.0 or self._last_error:
            self._error_streak = 0
            self._next_allowed_epoch = 0.0
            self._last_error_kind = ""
            self._last_error = ""
            self._save_backoff()

    async def _fetch_gce_instances(self) -> tuple[tuple[GceInstanceInfo, ...], Optional[str]]:
        res = await run_exec(
            [
                self.gcloud_bin,
                "compute",
                "instances",
                "list",
                f"--project={self.gcp_project}",
                "--format=json",
            ],
            semaphore=self.semaphore,
            timeout=12.0,
        )
        if res.timed_out or res.returncode != 0:
            return (), (res.stderr.strip() or f"gcloud failed ({res.returncode})")
        try:
            raw_list = json.loads(res.stdout or "[]")
        except json.JSONDecodeError as exc:
            return (), f"gcloud JSON decode error: {exc}"

        instances: list[GceInstanceInfo] = []
        for item in raw_list:
            zone_raw = str(item.get("zone", ""))
            mtype_raw = str(item.get("machineType", ""))
            instances.append(
                GceInstanceInfo(
                    name=str(item.get("name", "")),
                    zone=zone_raw.rsplit("/", 1)[-1],
                    status=str(item.get("status", "UNKNOWN")),
                    machine_type=mtype_raw.rsplit("/", 1)[-1],
                    created_at=str(item.get("creationTimestamp", "")),
                )
            )
        return tuple(instances), None

    async def collect(
        self,
        *,
        force: bool = False,
        now_utc: Optional[datetime] = None,
    ) -> ColabGcpSnapshot:
        now = now_utc or datetime.now(timezone.utc)
        now_iso = now.isoformat()
        now_ep = now.timestamp()
        mono = time.monotonic()
        caps = self.drive.capabilities()
        raw_backend = str(caps.get("backend") or "rclone").lower()
        drive_backend = "gdrive" if "gdrive" in raw_backend else "rclone"
        drive_configured = caps.get("configured") is not False
        colab_configured = self.colab_gateway is not None and self.colab_gateway.is_configured()

        if not drive_configured and not colab_configured:
            self._in_backoff_skip = True
            return ColabGcpSnapshot(
                fetched_at=now_iso,
                colab_state="NOT_CONFIGURED",
                rclone_readonly=bool(caps.get("readonly", False)),
                rclone_shared_client_id=bool(caps.get("shared_client_id", False)),
                drive_backend=drive_backend,
                colab_source="drive",
                stale=False,
            )

        drive_in_backoff = (not force) and (now_ep < self._next_allowed_epoch)
        if not colab_configured and drive_in_backoff:
            self._in_backoff_skip = True
            if self._last_snapshot is not None:
                return ColabGcpSnapshot(
                    **{
                        **self._last_snapshot.__dict__,
                        "fetched_at": now_iso,
                        "colab_state": "DEGRADED",
                        "drive_backend": drive_backend,
                        "error": self._last_error or self._last_snapshot.error,
                        "stale": True,
                    }
                )
            return ColabGcpSnapshot(
                fetched_at=now_iso,
                colab_state="DEGRADED",
                rclone_readonly=bool(caps.get("readonly", False)),
                rclone_shared_client_id=bool(caps.get("shared_client_id", False)),
                drive_backend=drive_backend,
                colab_source="drive",
                error=self._last_error or "Drive backoff cooldown active",
                stale=True,
            )

        self._in_backoff_skip = drive_in_backoff or (not drive_configured)
        errors: list[str] = []

        raw_status = ""
        colab_state = "DEGRADED"
        project = ""
        mode = ""
        reason = ""
        updated_str = ""
        age_seconds: Optional[int] = None
        idle_remaining: Optional[int] = None
        webui_url = ""
        colab_sessions_count: Optional[int] = None
        colab_gpu = ""
        colab_source = "drive"
        colab_ctrl_ok = False
        colab_drive_ok = False

        # 1. Authoritative control-plane probe (`colab --auth=adc sessions`) — never blocked by Drive backoff
        if colab_configured and self.colab_gateway is not None:
            try:
                sessions = await self.colab_gateway.list_sessions()
                colab_ctrl_ok = True
                colab_source = "colab_cli"
                colab_sessions_count = len(sessions)
                if sessions:
                    colab_state = "ONLINE"
                    colab_gpu = str(sessions[0].get("hardware") or "").strip()
                    raw_status = str(sessions[0].get("status") or "ONLINE").strip()
                else:
                    colab_state = "STOPPED"
                    colab_gpu = ""
                    raw_status = "STOPPED"
                    idle_remaining = 0
            except Exception as exc:
                errors.append(f"ColabCLI: {exc}")

        # 2. GCE Spot instances — never blocked by Drive backoff
        gce_instances, gce_err = await self._fetch_gce_instances()
        if gce_err:
            errors.append(f"GCE: {gce_err}")
            if self._last_snapshot is not None:
                gce_instances = self._last_snapshot.gce_instances

        # 3. Drive data-plane (`status.json` enrichment & storage quota) — respects Drive backoff
        drive_status_err: Optional[str] = None
        if drive_configured and not drive_in_backoff:
            try:
                status_text = await self.drive.cat_text(self.colab_status_path)
                status_json: dict[str, Any] = json.loads(status_text)
                colab_drive_ok = True
                d_raw_status = str(status_json.get("status", "")).strip()
                project = str(status_json.get("project", "")).strip()
                mode = str(status_json.get("mode", "")).strip()
                reason = str(status_json.get("reason", "")).strip()
                updated_str = str(
                    status_json.get("updated_at") or status_json.get("stopped_at") or ""
                ).strip()
                webui_url = str(status_json.get("webui_url") or status_json.get("api_url") or "").strip()

                parsed_ts = parse_colab_timestamp(updated_str)
                if parsed_ts is not None:
                    age_seconds = max(0, int((now - parsed_ts).total_seconds()))

                if colab_ctrl_ok:
                    colab_source = "hybrid"
                    if colab_state == "ONLINE":
                        if (
                            d_raw_status.upper() == "ONLINE"
                            and age_seconds is not None
                            and age_seconds <= STALE_THRESHOLD_SECONDS
                            and status_json.get("idle_seconds") is not None
                        ):
                            idle_sec = int(status_json.get("idle_seconds") or 0)
                            idle_remaining = max(0, IDLE_LIMIT_SECONDS - idle_sec)
                else:
                    raw_status = d_raw_status
                    if raw_status.upper().startswith("STOPPED"):
                        colab_state = "STOPPED"
                        idle_remaining = 0
                    elif raw_status.upper() == "ONLINE":
                        if age_seconds is None or age_seconds > STALE_THRESHOLD_SECONDS:
                            colab_state = "STALE_OFFLINE"
                        else:
                            colab_state = "ONLINE"
                        idle_sec = int(status_json.get("idle_seconds") or 0)
                        idle_remaining = max(0, IDLE_LIMIT_SECONDS - idle_sec)
                    else:
                        colab_state = raw_status or "UNKNOWN"
            except Exception as exc:
                drive_status_err = f"Drive: {exc}" if colab_ctrl_ok else f"Colab: {exc}"
                errors.append(drive_status_err)

            if force or (mono - self._last_about_ts >= self.about_ttl_sec) or self._last_about["total"] is None:
                try:
                    about_data = await self.drive.about()
                    if isinstance(about_data, dict) and any(
                        about_data.get(k) is not None for k in ("used", "total", "free")
                    ):
                        self._last_about = {
                            "used": int(about_data["used"]) if about_data.get("used") is not None else None,
                            "total": int(about_data["total"]) if about_data.get("total") is not None else None,
                            "free": int(about_data["free"]) if about_data.get("free") is not None else None,
                        }
                        self._last_about_ts = mono
                except Exception as exc:
                    errors.append(f"Drive: {exc}")

            if colab_drive_ok:
                self._reset_drive_backoff()
            elif drive_status_err:
                self._record_drive_failure(drive_status_err, now_ep)
        elif drive_configured and drive_in_backoff:
            errors.append(f"Drive: {self._last_error or 'Drive backoff cooldown active'}")
            if self._last_snapshot is not None:
                project = self._last_snapshot.project
                mode = self._last_snapshot.mode
                reason = self._last_snapshot.reason
                updated_str = self._last_snapshot.updated_at
                webui_url = self._last_snapshot.webui_url

        if colab_ctrl_ok:
            snap = ColabGcpSnapshot(
                fetched_at=now_iso,
                colab_state=colab_state,
                raw_status=raw_status,
                project=project,
                mode=mode,
                reason=reason,
                updated_at=updated_str,
                age_seconds=age_seconds,
                idle_remaining_seconds=idle_remaining,
                webui_url=webui_url,
                gce_instances=gce_instances,
                drive_used_bytes=self._last_about["used"],
                drive_total_bytes=self._last_about["total"],
                drive_free_bytes=self._last_about["free"],
                rclone_readonly=bool(caps.get("readonly", False)),
                rclone_shared_client_id=bool(caps.get("shared_client_id", False)),
                drive_backend=drive_backend,
                colab_sessions_count=colab_sessions_count,
                colab_gpu=colab_gpu,
                colab_source=colab_source,
                error="; ".join(errors) if errors else None,
                stale=False,
            )
            self._last_snapshot = snap
            return snap

        # Legacy Drive-only fallback path (control plane unavailable or unconfigured)
        if not colab_drive_ok and self._last_snapshot is not None:
            return ColabGcpSnapshot(
                fetched_at=now_iso,
                colab_state="DEGRADED",
                raw_status=self._last_snapshot.raw_status,
                project=self._last_snapshot.project,
                mode=self._last_snapshot.mode,
                reason=self._last_snapshot.reason,
                updated_at=self._last_snapshot.updated_at,
                age_seconds=self._last_snapshot.age_seconds,
                idle_remaining_seconds=self._last_snapshot.idle_remaining_seconds,
                webui_url=self._last_snapshot.webui_url,
                gce_instances=gce_instances,
                drive_used_bytes=self._last_about["used"],
                drive_total_bytes=self._last_about["total"],
                drive_free_bytes=self._last_about["free"],
                rclone_readonly=bool(caps.get("readonly", False)),
                rclone_shared_client_id=bool(caps.get("shared_client_id", False)),
                drive_backend=drive_backend,
                colab_sessions_count=self._last_snapshot.colab_sessions_count,
                colab_gpu=self._last_snapshot.colab_gpu,
                colab_source="drive",
                error="; ".join(errors) if errors else "Colab probe failed",
                stale=True,
            )

        snap = ColabGcpSnapshot(
            fetched_at=now_iso,
            colab_state=colab_state,
            raw_status=raw_status,
            project=project,
            mode=mode,
            reason=reason,
            updated_at=updated_str,
            age_seconds=age_seconds,
            idle_remaining_seconds=idle_remaining,
            webui_url=webui_url,
            gce_instances=gce_instances,
            drive_used_bytes=self._last_about["used"],
            drive_total_bytes=self._last_about["total"],
            drive_free_bytes=self._last_about["free"],
            rclone_readonly=bool(caps.get("readonly", False)),
            rclone_shared_client_id=bool(caps.get("shared_client_id", False)),
            drive_backend=drive_backend,
            colab_sessions_count=colab_sessions_count,
            colab_gpu=colab_gpu,
            colab_source="drive",
            error="; ".join(errors) if errors else None,
            stale=not colab_drive_ok,
        )
        if colab_drive_ok:
            self._last_snapshot = snap
        return snap
