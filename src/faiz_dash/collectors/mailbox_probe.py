"""Colab Mailbox queue depth, failure preview, and anti-false-done verification."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from typing import Any, Callable, Optional
import urllib.request

from ..gateways.drive import DriveGateway
from ..models import MailboxItem, MailboxSnapshot, redact_secrets
from ..statusline_config import get_cloud_settings


def _http_get_json_sync(url: str, timeout: float = 2.0) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)


def _extract_tail(text: str, max_lines: int = 3) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    return redact_secrets(" | ".join(lines[-max_lines:]))[:240]


class MailboxProbe:
    """Scans mailbox/{tasks,done,failed} and verifies real Drive/ComfyUI deliverables."""

    def __init__(
        self,
        drive: DriveGateway,
        *,
        mailbox_root: Optional[str] = None,
        projects_root: Optional[str] = None,
        http_get_json: Optional[Callable[[str, float], dict[str, Any]]] = None,
    ) -> None:
        cloud_cfg = get_cloud_settings()
        self.drive = drive
        self.mailbox_root = mailbox_root or cloud_cfg["mailbox_root"]
        self.projects_root = projects_root or cloud_cfg["projects_root"]
        self._http_get_json = http_get_json or _http_get_json_sync
        self._task_meta_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._last_snapshot: Optional[MailboxSnapshot] = None

    async def _get_task_meta(self, queue: str, name: str, modified_at: str) -> dict[str, Any]:
        key = (f"{queue}/{name}", modified_at)
        if key in self._task_meta_cache:
            return self._task_meta_cache[key]
        try:
            raw = await self.drive.cat_text(f"{self.mailbox_root}/{queue}/{name}")
            meta = json.loads(raw)
            if isinstance(meta, dict):
                self._task_meta_cache[key] = meta
                return meta
        except Exception:
            pass
        return {}

    async def _verify_tts_done(
        self,
        item: dict[str, Any],
        default_project: str,
    ) -> tuple[bool, str]:
        name = item["name"]
        meta = await self._get_task_meta("done", name, item.get("modified_at", ""))
        job_id = str(meta.get("job_id") or name.removesuffix(".tts.json").removesuffix(".json"))
        proj = str(meta.get("project_name") or meta.get("project") or default_project).strip()
        if not proj:
            return False, "missing project name"

        explicit_sub = str(meta.get("out_subdir") or "").strip()
        candidate_subs = [explicit_sub] if explicit_sub else ["03_Audio/cloud_raw", "04_Audio/cloud_raw"]
        for sub in candidate_subs:
            target_dir = f"{self.projects_root}/{proj}/{sub}/{job_id}"
            try:
                entries = await self.drive.lsl(target_dir, max_depth=1)
            except Exception:
                continue
            receipt = f"{job_id}_receipt.json"
            if any(e["name"] == receipt and e["size"] > 0 for e in entries):
                wav_count = sum(1 for e in entries if e["name"].endswith(".wav") and e["size"] > 0)
                return True, f"{sub}: receipt + {wav_count} wav"
        return False, f"no {job_id}_receipt.json in cloud_raw"

    async def _verify_comfy_done(
        self,
        item: dict[str, Any],
        default_project: str,
        colab_online: bool,
        webui_url: str,
    ) -> tuple[bool, str]:
        name = item["name"]
        meta = await self._get_task_meta("done", name, item.get("modified_at", ""))
        proj = str(meta.get("project_name") or meta.get("project") or default_project).strip()
        task_epoch = float(item.get("mtime_epoch") or 0.0)

        if proj:
            for subdir in ("03_Footage", "05_Delivery"):
                target_dir = f"{self.projects_root}/{proj}/{subdir}"
                try:
                    entries = await self.drive.lsl(target_dir, max_depth=2)
                except Exception:
                    continue
                valid_files = [
                    e
                    for e in entries
                    if e["size"] > 0 and (task_epoch <= 0.0 or e["mtime_epoch"] >= task_epoch - 60.0)
                ]
                if valid_files:
                    newest = max(valid_files, key=lambda e: e["mtime_epoch"])
                    return True, f"{subdir}/{newest['name']} ({newest['size']}B)"

        prompt_id = str(meta.get("prompt_id") or "").strip()
        if colab_online and webui_url and prompt_id:
            url = f"{webui_url.rstrip('/')}/history/{prompt_id}"
            try:
                hist = await asyncio.to_thread(self._http_get_json, url, 2.0)
                entry = hist.get(prompt_id, hist) if isinstance(hist, dict) else {}
                status_obj = entry.get("status", {}) if isinstance(entry, dict) else {}
                if status_obj.get("completed") is True:
                    return True, f"comfy history completed ({prompt_id[:8]})"
                return False, "UNVERIFIED_OR_FAILED (history incomplete)"
            except Exception as exc:
                return False, f"UNVERIFIED_OR_FAILED (history err: {exc})"

        return False, "UNVERIFIED_OR_FAILED (no footage/delivery >0B)"

    async def _verify_done_entry(
        self,
        entry: dict[str, Any],
        default_project: str,
        colab_online: bool,
        webui_url: str,
    ) -> MailboxItem:
        dname = entry["name"]
        is_tts = dname.endswith(".tts.json")
        if is_tts:
            ok, detail = await self._verify_tts_done(entry, default_project)
        else:
            ok, detail = await self._verify_comfy_done(
                entry, default_project, colab_online, webui_url
            )
        return MailboxItem(
            name=dname,
            queue="done",
            size=entry["size"],
            modified_at=entry["modified_at"],
            is_tts=is_tts,
            verified=ok,
            verify_detail=detail,
        )

    async def collect(
        self,
        *,
        default_project: str = "",
        colab_online: bool = False,
        webui_url: str = "",
        verify_limit: int = 3,
        skip_drive: bool = False,
        skip_error: Optional[str] = None,
    ) -> MailboxSnapshot:
        now_iso = datetime.now(timezone.utc).isoformat()
        if skip_drive:
            if self._last_snapshot is not None:
                return MailboxSnapshot(
                    fetched_at=now_iso,
                    tasks_count=self._last_snapshot.tasks_count,
                    done_count=self._last_snapshot.done_count,
                    failed_count=self._last_snapshot.failed_count,
                    tasks=self._last_snapshot.tasks,
                    done_items=self._last_snapshot.done_items,
                    failed_items=self._last_snapshot.failed_items,
                    error=skip_error or "Drive backoff active",
                    stale=True,
                )
            return MailboxSnapshot(fetched_at=now_iso, error=skip_error or "Drive backoff active", stale=True)

        try:
            tasks_raw, done_raw, failed_raw = await asyncio.gather(
                self.drive.lsl(f"{self.mailbox_root}/tasks", max_depth=1),
                self.drive.lsl(f"{self.mailbox_root}/done", max_depth=1),
                self.drive.lsl(f"{self.mailbox_root}/failed", max_depth=1),
            )
        except Exception as exc:
            if self._last_snapshot is not None:
                return MailboxSnapshot(
                    fetched_at=now_iso,
                    tasks_count=self._last_snapshot.tasks_count,
                    done_count=self._last_snapshot.done_count,
                    failed_count=self._last_snapshot.failed_count,
                    tasks=self._last_snapshot.tasks,
                    done_items=self._last_snapshot.done_items,
                    failed_items=self._last_snapshot.failed_items,
                    error=str(exc),
                    stale=True,
                )
            return MailboxSnapshot(fetched_at=now_iso, error=str(exc), stale=True)

        # Tasks queue
        task_jsons = sorted(
            [e for e in tasks_raw if e["name"].endswith(".json")],
            key=lambda e: e["mtime_epoch"],
            reverse=True,
        )
        task_items = tuple(
            MailboxItem(
                name=e["name"],
                queue="tasks",
                size=e["size"],
                modified_at=e["modified_at"],
                is_tts=e["name"].endswith(".tts.json"),
            )
            for e in task_jsons
        )

        # Failed queue: pair `.log` files with their matching task, read up to 3 log tails
        failed_logs = {e["name"]: e for e in failed_raw if e["name"].endswith(".log")}
        failed_jsons = sorted(
            [e for e in failed_raw if not e["name"].endswith(".log")],
            key=lambda e: e["mtime_epoch"],
            reverse=True,
        )
        if not failed_jsons and failed_logs:
            failed_jsons = sorted(failed_logs.values(), key=lambda e: e["mtime_epoch"], reverse=True)

        failed_items_list: list[MailboxItem] = []
        for idx, entry in enumerate(failed_jsons):
            fname = entry["name"]
            log_name = fname if fname.endswith(".log") else f"{fname}.log"
            log_tail = ""
            if idx < 3 and log_name in failed_logs:
                try:
                    log_text = await self.drive.cat_text(f"{self.mailbox_root}/failed/{log_name}")
                    log_tail = _extract_tail(log_text, max_lines=3)
                except Exception as exc:
                    log_tail = f"log read failed: {exc}"
            failed_items_list.append(
                MailboxItem(
                    name=fname,
                    queue="failed",
                    size=entry["size"],
                    modified_at=entry["modified_at"],
                    is_tts=fname.endswith(".tts.json"),
                    verified=False,
                    verify_detail="failed",
                    log_tail=log_tail,
                )
            )

        # Done queue: verify top N newest `.json` items concurrently (bounded by Drive semaphore)
        done_jsons = sorted(
            [e for e in done_raw if e["name"].endswith(".json")],
            key=lambda e: e["mtime_epoch"],
            reverse=True,
        )
        done_items_list = list(
            await asyncio.gather(
                *(
                    self._verify_done_entry(entry, default_project, colab_online, webui_url)
                    for entry in done_jsons[:verify_limit]
                )
            )
        )

        snap = MailboxSnapshot(
            fetched_at=now_iso,
            tasks_count=len(task_items),
            done_count=len(done_jsons),
            failed_count=len(failed_items_list),
            tasks=task_items,
            done_items=tuple(done_items_list),
            failed_items=tuple(failed_items_list),
            error=None,
            stale=False,
        )
        self._last_snapshot = snap
        return snap
