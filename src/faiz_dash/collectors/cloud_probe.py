"""Colab CLI control-plane, boot.sh status.json, GCE Spot instances, and Drive quota collector."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Optional
import urllib.error
import urllib.parse
import urllib.request

from ..gateways.colab import ColabCliGateway
from ..gateways.drive import DriveGateway
from ..gateways.exec_util import resolve_binary, run_exec
from ..models import ColabGcpSnapshot, GceInstanceInfo, redact_secrets
from ..statusline_config import get_cloud_settings
from ..store import DEFAULT_CACHE_DIR, atomic_write_json

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


def _atomic_write_secret_json(path: Path, payload: dict[str, Any]) -> None:
    """Write sensitive OAuth token JSON atomically with 0o600 permissions (no secret redaction)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.tmp.{os.getpid()}"
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    fd = -1
    try:
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp_path), str(path))
    except Exception:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


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
        colab_usage_ttl_sec: float = 90.0,
        backoff_path: Optional[Path] = None,
        cache_dir: Optional[Path] = None,
        adc_path: Optional[Path] = None,
    ) -> None:
        cloud_cfg = get_cloud_settings()
        self.drive = drive
        self.colab_gateway = colab_gateway
        self.gcloud_bin = gcloud_bin
        self.gcp_project = gcp_project or cloud_cfg["gcp_project"]
        self.colab_status_path = colab_status_path or cloud_cfg["colab_status_path"]
        self.semaphore = semaphore
        self.about_ttl_sec = about_ttl_sec
        self.colab_usage_ttl_sec = colab_usage_ttl_sec
        self.backoff_path = backoff_path
        self.cache_dir = cache_dir or (backoff_path.parent if backoff_path is not None else DEFAULT_CACHE_DIR)
        self._explicit_adc_path = adc_path

        self._last_snapshot: Optional[ColabGcpSnapshot] = None
        self._last_about: dict[str, Optional[int]] = {"used": None, "total": None, "free": None}
        self._last_about_ts: float = 0.0
        self._last_colab_balance_cu: Optional[float] = None
        self._last_colab_usage_rate: str = ""
        self._last_colab_usage_ts: float = 0.0
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

    def _resolve_adc_path(self) -> Path:
        if self._explicit_adc_path is not None:
            return self._explicit_adc_path
        env_creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        if env_creds:
            return Path(env_creds).expanduser()
        return Path.home() / ".config" / "gcloud" / "application_default_credentials.json"

    def _resolve_gcp_project(self, adc_data: Optional[dict[str, Any]] = None) -> str:
        proj = (self.gcp_project or "").strip()
        if proj and proj != "your-gcp-project":
            return proj
        for env_key in ("GOOGLE_CLOUD_PROJECT", "CLOUDSDK_CORE_PROJECT"):
            val = os.environ.get(env_key, "").strip()
            if val and val != "your-gcp-project":
                return val
        if adc_data:
            qp = str(adc_data.get("quota_project_id") or "").strip()
            if qp and qp != "your-gcp-project":
                return qp
        return ""

    def _read_or_refresh_adc_token(
        self,
        *,
        force_refresh: bool = False,
    ) -> tuple[Optional[str], str, Optional[str]]:
        """Return (access_token, quota_project_id, status_or_error).

        status_or_error == "fallback" signals non-authorized_user / missing ADC -> use gcloud CLI.
        """
        adc_file = self._resolve_adc_path()
        if not adc_file.is_file():
            return None, "", "fallback"
        try:
            adc_stat = adc_file.stat()
            adc_data = json.loads(adc_file.read_text(encoding="utf-8"))
        except Exception:
            return None, "", "fallback"
        if not isinstance(adc_data, dict) or adc_data.get("type") != "authorized_user":
            return None, "", "fallback"

        client_id = str(adc_data.get("client_id") or "").strip()
        client_secret = str(adc_data.get("client_secret") or "").strip()
        refresh_token = str(adc_data.get("refresh_token") or "").strip()
        quota_proj = str(adc_data.get("quota_project_id") or "").strip()
        if not (client_id and client_secret and refresh_token):
            return None, quota_proj, "fallback"

        tok_cache_path = self.cache_dir / ".gcp_adc_token.json"
        now_ep = time.time()
        if not force_refresh and tok_cache_path.is_file():
            try:
                cached = json.loads(tok_cache_path.read_text(encoding="utf-8"))
                if (
                    isinstance(cached, dict)
                    and cached.get("access_token")
                    and float(cached.get("expires_at") or 0.0) > now_ep
                    and cached.get("client_id") == client_id
                    and float(cached.get("adc_mtime") or 0.0) == adc_stat.st_mtime
                ):
                    return str(cached["access_token"]), quota_proj, None
            except Exception:
                pass

        body = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=6.0) as resp:
                tok_resp = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return None, quota_proj, redact_secrets(f"ADC OAuth HTTP {exc.code}")
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            return None, quota_proj, redact_secrets(f"ADC OAuth error: {type(exc).__name__}")

        access_token = str(tok_resp.get("access_token") or "").strip()
        if not access_token:
            return None, quota_proj, "ADC OAuth missing access_token"
        try:
            expires_in = int(tok_resp.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        ttl = max(60, min(expires_in, 3600) - 300)
        try:
            _atomic_write_secret_json(
                tok_cache_path,
                {
                    "access_token": access_token,
                    "expires_at": int(now_ep + ttl),
                    "client_id": client_id,
                    "adc_mtime": adc_stat.st_mtime,
                },
            )
        except OSError:
            pass
        return access_token, quota_proj, None

    def _fetch_gce_instances_rest_sync(self) -> tuple[Optional[tuple[GceInstanceInfo, ...]], Optional[str]]:
        """Query Compute Engine aggregated/instances REST API using cached ADC token."""
        adc_file = self._resolve_adc_path()
        adc_data: Optional[dict[str, Any]] = None
        if adc_file.is_file():
            try:
                raw_adc = json.loads(adc_file.read_text(encoding="utf-8"))
                if isinstance(raw_adc, dict):
                    adc_data = raw_adc
            except Exception:
                adc_data = None

        project = self._resolve_gcp_project(adc_data)
        if not project:
            return (), None

        token, quota_proj, tok_err = self._read_or_refresh_adc_token(force_refresh=False)
        if tok_err == "fallback":
            return None, "fallback"
        if tok_err or not token:
            return None, tok_err or "ADC token unavailable"

        instances: list[GceInstanceInfo] = []
        page_token = ""
        retried_401 = False
        pages = 0

        while pages < 5:
            pages += 1
            params: dict[str, str] = {
                "filter": '(status="RUNNING" OR status="STAGING")',
                "returnPartialSuccess": "true",
                "fields": (
                    "nextPageToken,"
                    "items/*/instances(name,zone,machineType,creationTimestamp,status,scheduling/provisioningModel)"
                ),
            }
            if page_token:
                params["pageToken"] = page_token
            url = (
                f"https://compute.googleapis.com/compute/v1/projects/"
                f"{urllib.parse.quote(project, safe='')}/aggregated/instances?"
                f"{urllib.parse.urlencode(params)}"
            )
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            if quota_proj:
                headers["x-goog-user-project"] = quota_proj
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=6.0) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and not retried_401:
                    retried_401 = True
                    try:
                        tok_path = self.cache_dir / ".gcp_adc_token.json"
                        if tok_path.exists():
                            tok_path.unlink()
                    except OSError:
                        pass
                    token, quota_proj, tok_err = self._read_or_refresh_adc_token(force_refresh=True)
                    if tok_err or not token:
                        return None, tok_err or "GCE REST 401 refresh failed"
                    pages -= 1
                    continue
                return None, redact_secrets(f"Compute REST HTTP {exc.code}")
            except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
                return None, redact_secrets(f"Compute REST {type(exc).__name__}")

            if not isinstance(payload, dict):
                break
            items_map = payload.get("items") or {}
            if isinstance(items_map, dict):
                for zone_obj in items_map.values():
                    if not isinstance(zone_obj, dict):
                        continue
                    for item in (zone_obj.get("instances") or ()):
                        if not isinstance(item, dict):
                            continue
                        zone_raw = str(item.get("zone", ""))
                        mtype_raw = str(item.get("machineType", ""))
                        sched = item.get("scheduling")
                        prov = str(sched.get("provisioningModel") or "") if isinstance(sched, dict) else ""
                        instances.append(
                            GceInstanceInfo(
                                name=str(item.get("name", "")),
                                zone=zone_raw.rsplit("/", 1)[-1],
                                status=str(item.get("status", "UNKNOWN")),
                                machine_type=mtype_raw.rsplit("/", 1)[-1],
                                created_at=str(item.get("creationTimestamp", "")),
                                provisioning_model=prov,
                            )
                        )
            page_token = str(payload.get("nextPageToken") or "").strip()
            if not page_token:
                break

        return tuple(instances), None

    async def _fetch_gce_instances(self) -> tuple[tuple[GceInstanceInfo, ...], Optional[str]]:
        rest_instances, rest_err = await asyncio.to_thread(self._fetch_gce_instances_rest_sync)
        if rest_instances is not None:
            return rest_instances, None

        project = self._resolve_gcp_project()
        if not project:
            return (), None
        if not self.gcloud_bin:
            return (), (None if rest_err == "fallback" else rest_err)

        res = await run_exec(
            [
                self.gcloud_bin,
                "compute",
                "instances",
                "list",
                f"--project={project}",
                "--filter=status=(RUNNING,STAGING)",
                "--format=json",
            ],
            semaphore=self.semaphore,
            timeout=12.0,
        )
        if res.timed_out or res.returncode != 0:
            err_txt = res.stderr.strip() or f"gcloud failed ({res.returncode})"
            return (), (f"{rest_err}; {err_txt}" if rest_err and rest_err != "fallback" else err_txt)
        try:
            raw_list = json.loads(res.stdout or "[]")
        except json.JSONDecodeError as exc:
            return (), f"gcloud JSON decode error: {exc}"

        instances: list[GceInstanceInfo] = []
        for item in raw_list:
            zone_raw = str(item.get("zone", ""))
            mtype_raw = str(item.get("machineType", ""))
            sched = item.get("scheduling")
            prov = str(sched.get("provisioningModel") or "") if isinstance(sched, dict) else ""
            instances.append(
                GceInstanceInfo(
                    name=str(item.get("name", "")),
                    zone=zone_raw.rsplit("/", 1)[-1],
                    status=str(item.get("status", "UNKNOWN")),
                    machine_type=mtype_raw.rsplit("/", 1)[-1],
                    created_at=str(item.get("creationTimestamp", "")),
                    provisioning_model=prov,
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

            # Best-effort CU balance enrichment (isolated: never degrades ONLINE state or pollutes snap.error)
            if (
                force
                or (mono - self._last_colab_usage_ts >= self.colab_usage_ttl_sec)
                or self._last_colab_balance_cu is None
            ):
                try:
                    usage_info = await self.colab_gateway.get_usage()
                    if isinstance(usage_info, dict) and usage_info:
                        if usage_info.get("balance_cu") is not None:
                            self._last_colab_balance_cu = float(usage_info["balance_cu"])
                        if usage_info.get("usage_rate"):
                            self._last_colab_usage_rate = str(usage_info["usage_rate"])
                        self._last_colab_usage_ts = mono
                except Exception:
                    pass

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
                colab_balance_cu=self._last_colab_balance_cu,
                colab_usage_rate=self._last_colab_usage_rate,
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
                colab_balance_cu=self._last_colab_balance_cu,
                colab_usage_rate=self._last_colab_usage_rate,
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
            colab_balance_cu=self._last_colab_balance_cu,
            colab_usage_rate=self._last_colab_usage_rate,
            error="; ".join(errors) if errors else None,
            stale=not colab_drive_ok,
        )
        if colab_drive_ok:
            self._last_snapshot = snap
        return snap
