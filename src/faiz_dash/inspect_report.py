"""Read-only diagnostic snapshot & Markdown bubble formatter for Faiz Control Center."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Optional

from .models import sanitize_for_json
from .store import (
    WINDOW_5H_SECONDS,
    WINDOW_WEEKLY_SECONDS,
    StateStore,
    compute_quota_pace,
    parse_refresh_seconds,
    read_backoff_remaining,
    remaining_reset_seconds,
    summarize_probe_error,
)

def _default_telemetry_script() -> Path:
    return Path.home() / ".agents" / "scripts" / "telemetry.py"


def _format_tokens(val: int) -> str:
    if val >= 1_000_000:
        return f"{val / 1_000_000:.2f}M"
    if val >= 1_000:
        return f"{val / 1_000:.1f}k"
    return str(val)


def _format_bytes(num: Optional[int]) -> str:
    if num is None:
        return "--"
    gib = num / (1024**3)
    if gib >= 1024:
        return f"{gib / 1024:.2f}T"
    return f"{gib:.1f}G"


def _format_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return "--"
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}h{mins:02d}m"
    if mins > 0:
        return f"{mins}m{secs:02d}s"
    return f"{secs}s"


def _format_forecast_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return "--"
    total = max(0, int(seconds))
    if total >= 86400:
        days, rem = divmod(total, 86400)
        hours = rem // 3600
        return f"{days}d{hours}h"
    return _format_duration(total)


def _format_reset_window(refresh_text: str, anchor_epoch: int, now_epoch: float) -> str:
    text = (refresh_text or "").strip()
    if not text:
        return "--"
    if "available" in text.lower():
        return "available"
    secs = parse_refresh_seconds(text)
    if secs is not None and anchor_epoch > 0:
        rem = (anchor_epoch + secs) - int(now_epoch)
        if rem <= 0:
            return "due"
        return _format_duration(rem)
    if text.lower().startswith("refreshes in "):
        return text[13:].strip()
    return text


def _file_age_seconds(path: Path, now_epoch: float) -> Optional[int]:
    if not path.exists():
        return None
    try:
        return max(0, int(now_epoch - path.stat().st_mtime))
    except OSError:
        return None


def _clean_log_tail(raw_tail: str) -> str:
    if not raw_tail:
        return ""
    lines = [
        ln.strip()
        for ln in raw_tail.splitlines()
        if ln.strip() and "NOTICE: gdrive:" not in ln and "shared Google Drive client_id" not in ln
    ]
    core = lines[-1] if lines else raw_tail.splitlines()[0].strip()
    if len(core) > 100:
        core = core[:97] + "..."
    return core


def _read_telemetry_sqlite_readonly(db_path: Path) -> Optional[dict[str, Any]]:
    if not db_path.exists():
        return None
    empty_metrics: dict[str, Any] = {
        "total_tasks": 0,
        "completed_tasks": 0,
        "failed_tasks": 0,
        "success_rate_percent": 100.0,
        "avg_duration_sec": 0.0,
        "avg_turns_per_task": 1.0,
        "total_tools_called": 0,
        "t1_interceptions_count": 0,
        "top_tools": [],
    }
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "tasks" not in tables or "tool_invocations" not in tables:
            return empty_metrics
        total_tasks = int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] or 0)
        completed_tasks = int(conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'COMPLETED'").fetchone()[0] or 0)
        failed_tasks = int(conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'FAILED'").fetchone()[0] or 0)
        avg_duration = float(conn.execute("SELECT AVG(duration_ms) FROM tasks WHERE status = 'COMPLETED'").fetchone()[0] or 0.0)
        avg_turns = float(conn.execute("SELECT AVG(total_turns) FROM tasks WHERE status = 'COMPLETED'").fetchone()[0] or 1.0)
        total_tools = int(conn.execute("SELECT COUNT(*) FROM tool_invocations").fetchone()[0] or 0)
        intercepted_tools = int(conn.execute("SELECT COUNT(*) FROM tool_invocations WHERE intercepted_by_t1 = 1").fetchone()[0] or 0)
        top_tools = conn.execute(
            "SELECT tool_name, COUNT(*) as count FROM tool_invocations GROUP BY tool_name ORDER BY count DESC LIMIT 5"
        ).fetchall()
        success_rate = (completed_tasks / total_tasks * 100.0) if total_tasks > 0 else 100.0
        return sanitize_for_json(
            {
                "total_tasks": total_tasks,
                "completed_tasks": completed_tasks,
                "failed_tasks": failed_tasks,
                "success_rate_percent": round(success_rate, 1),
                "avg_duration_sec": round(avg_duration / 1000.0, 2),
                "avg_turns_per_task": round(avg_turns, 1),
                "total_tools_called": total_tools,
                "t1_interceptions_count": intercepted_tools,
                "top_tools": [{"tool": r["tool_name"], "calls": r["count"]} for r in top_tools],
            }
        )
    except Exception:
        return None
    finally:
        if conn is not None:
            conn.close()


def read_telemetry_summary(
    *,
    telemetry_db: Optional[Path] = None,
    script_path: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """Safely load macro APM metrics in read-only mode from `telemetry.py` or `telemetry.db`."""
    target_script = script_path or _default_telemetry_script()
    if target_script.exists():
        try:
            spec = importlib.util.spec_from_file_location("_faiz_telemetry_ro", str(target_script))
            if spec is not None and spec.loader is not None:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                orig_db = getattr(mod, "DB_PATH", None)
                try:
                    if telemetry_db is not None:
                        setattr(mod, "DB_PATH", str(telemetry_db))
                    metrics = mod.get_summary_metrics(readonly=True)
                    if isinstance(metrics, dict):
                        return sanitize_for_json(metrics)
                finally:
                    if telemetry_db is not None and orig_db is not None:
                        setattr(mod, "DB_PATH", orig_db)
        except Exception:
            pass
    db_path = telemetry_db or (Path.home() / ".agents" / "telemetry.db")
    return _read_telemetry_sqlite_readonly(db_path)


def collect_inspect_payload(
    store: Optional[StateStore] = None,
    *,
    telemetry_db: Optional[Path] = None,
    telemetry_script: Optional[Path] = None,
    now_epoch: Optional[float] = None,
) -> dict[str, Any]:
    """Build a complete, secret-redacted diagnostic payload from StateStore + APM telemetry."""
    active_store = store or StateStore()
    now_ts = time.time() if now_epoch is None else float(now_epoch)
    backoff_sec = read_backoff_remaining(active_store.cache_dir / "cloud_backoff.json")

    quota_dict: Optional[dict[str, Any]] = None
    if active_store.quota is not None:
        q = active_store.quota
        quota_dict = sanitize_for_json(q)
        anchor = q.pty_fetched_epoch or 0
        enriched_buckets: list[dict[str, Any]] = []
        for bucket in q.buckets:
            b_dict = sanitize_for_json(bucket)
            b_dict["five_hour_remaining"] = _format_reset_window(bucket.five_hour_refresh, anchor, now_ts)
            b_dict["weekly_remaining"] = _format_reset_window(bucket.weekly_refresh, anchor, now_ts)
            rem_5h = remaining_reset_seconds(bucket.five_hour_refresh, anchor, now_ts)
            rem_wk = remaining_reset_seconds(bucket.weekly_refresh, anchor, now_ts)
            b_dict["five_hour_pace"] = compute_quota_pace(bucket.five_hour_pct, rem_5h, WINDOW_5H_SECONDS)
            b_dict["weekly_pace"] = compute_quota_pace(bucket.weekly_pct, rem_wk, WINDOW_WEEKLY_SECONDS)
            enriched_buckets.append(b_dict)
        quota_dict["buckets"] = enriched_buckets

    cloud_dict: Optional[dict[str, Any]] = None
    if active_store.cloud is not None:
        c = active_store.cloud
        cloud_dict = sanitize_for_json(c)
        cloud_dict["error_summary"] = summarize_probe_error(c.error, cooldown_sec=backoff_sec)

    mailbox_dict: Optional[dict[str, Any]] = None
    if active_store.mailbox is not None:
        m = active_store.mailbox
        mailbox_dict = sanitize_for_json(m)
        mailbox_dict["unverified_count"] = sum(1 for item in m.done_items if item.verified is False)
        mailbox_dict["error_summary"] = summarize_probe_error(m.error, cooldown_sec=backoff_sec)

    return {
        "generated_at_epoch": int(now_ts),
        "cache_dir": str(active_store.cache_dir),
        "cache_ages_sec": {
            "usage": _file_age_seconds(active_store.usage_cache_path, now_ts),
            "cloud": _file_age_seconds(active_store.cloud_cache_path, now_ts),
        },
        "backoff_remaining_sec": backoff_sec,
        "quota": quota_dict,
        "cloud": cloud_dict,
        "mailbox": mailbox_dict,
        "telemetry": read_telemetry_summary(
            telemetry_db=telemetry_db,
            script_path=telemetry_script,
        ),
    }


def _pct_cell(pct: Any) -> str:
    if pct is None:
        return "--"
    try:
        return f"{float(pct):.1f}%"
    except (TypeError, ValueError):
        return "--"


def _format_single_pace(label: str, pace: Optional[dict[str, Any]]) -> str:
    if not isinstance(pace, dict):
        return ""
    delta = int(pace.get("pace_delta_pct") or 0)
    sign_txt = f"{delta:+d}%" if delta != 0 else "0%"
    ro = pace.get("runs_out_seconds")
    if ro is not None:
        return f"{label} {sign_txt} (~{_format_forecast_duration(int(ro))})"
    return f"{label} {sign_txt}"


def _format_pace_cell(five_h_pace: Optional[dict[str, Any]], wk_pace: Optional[dict[str, Any]]) -> str:
    parts = [p for p in (_format_single_pace("5h", five_h_pace), _format_single_pace("Wk", wk_pace)) if p]
    return " / ".join(parts) if parts else "--"


def _render_quota_section(quota: Optional[dict[str, Any]], usage_age: Optional[int]) -> list[str]:
    age_txt = _format_duration(usage_age) if usage_age is not None else "missing"
    if not quota:
        return [
            "### 1. Quota & Burn",
            f"- No cached `/usage` snapshot found (age: `{age_txt}`). Run `faiz-dash inspect --live` to probe.",
        ]

    gate = quota.get("quota_gate", "UNKNOWN")
    ch = quota.get("channels") or {}
    pro_cnt = int(ch.get("pro_count") or 0)
    gcp_cnt = int(ch.get("gcp_count") or 0)
    auth_mode = ch.get("active_auth_method") or "--"
    acct = quota.get("account") or "--"
    src = quota.get("source") or "cache"
    stale_tag = " | stale" if quota.get("stale") else ""

    lines = [
        f"### 1. Quota & Burn (`{gate}` | age `{age_txt}` | source `{src}{stale_tag}`)",
        f"- Account: `{acct}` | Channels: `Pro={pro_cnt}`, `GCP={gcp_cnt}` (`{auth_mode}`)",
    ]
    buckets = quota.get("buckets") or []
    if buckets:
        lines.extend(
            [
                "",
                "| Bucket | 5h | 5h Reset | Weekly | Weekly Reset | Pace / Forecast |",
                "| :--- | :---: | :--- | :---: | :--- | :--- |",
            ]
        )
        for b in buckets:
            pace_cell = _format_pace_cell(b.get("five_hour_pace"), b.get("weekly_pace"))
            lines.append(
                f"| {b.get('group_name', '?')} | {_pct_cell(b.get('five_hour_pct'))} | "
                f"{b.get('five_hour_remaining', '--')} | {_pct_cell(b.get('weekly_pct'))} | "
                f"{b.get('weekly_remaining', '--')} | {pace_cell} |"
            )

    burn = quota.get("burn") or {}
    t1h = _format_tokens(int(burn.get("tokens_1h") or 0))
    t24h = _format_tokens(int(burn.get("tokens_24h") or 0))
    hit = float(burn.get("cache_hit_rate") or 0.0) * 100.0
    lines.append(f"- Global Burn (top 10): `1h {t1h}` | `24h {t24h}` | Prompt Cache Hit `{hit:.1f}%`")
    by_model = burn.get("by_model_24h") or {}
    if isinstance(by_model, dict) and by_model:
        ranked = sorted(by_model.items(), key=lambda kv: int(kv[1]), reverse=True)[:4]
        top_str = ", ".join(f"`{name}` {_format_tokens(int(val))}" for name, val in ranked)
        lines.append(f"- 24h Models: {top_str}")
    if quota.get("error"):
        lines.append(f"- Quota Note: `{summarize_probe_error(str(quota.get('error')))}`")
    return lines


def _render_cloud_section(
    cloud: Optional[dict[str, Any]],
    cloud_age: Optional[int],
    backoff_sec: int,
) -> list[str]:
    age_txt = _format_duration(cloud_age) if cloud_age is not None else "missing"
    if not cloud:
        return [
            "### 2. Cloud, GCE & Drive Sync",
            f"- No cached cloud snapshot found (age: `{age_txt}`).",
        ]

    state = cloud.get("colab_state", "UNKNOWN")
    idle = _format_duration(cloud.get("idle_remaining_seconds"))
    gces = cloud.get("gce_instances") or []
    running = [g for g in gces if str(g.get("status", "")).upper() in {"RUNNING", "STAGING"}]
    gce_txt = (
        f"{len(running)} running ({running[0].get('name', '?')})"
        if running
        else f"0 running (total {len(gces)})"
    )
    used_b = _format_bytes(cloud.get("drive_used_bytes"))
    total_b = _format_bytes(cloud.get("drive_total_bytes"))
    free_b = _format_bytes(cloud.get("drive_free_bytes"))
    backend = str(cloud.get("drive_backend") or "rclone")
    if backend == "gdrive":
        backend_txt = "gdrive: custom OAuth OK"
    else:
        flags: list[str] = []
        if cloud.get("rclone_readonly"):
            flags.append("RO")
        if cloud.get("rclone_shared_client_id"):
            flags.append("shared_client_id")
        backend_txt = f"rclone: {', '.join(flags)}" if flags else "rclone: custom OAuth OK"
    cd_txt = _format_duration(backoff_sec) if backoff_sec > 0 else "0s"
    src_tag = str(cloud.get("colab_source") or "drive")
    gpu_tag = str(cloud.get("colab_gpu") or "").strip()
    gpu_part = f" | GPU: `{gpu_tag}`" if gpu_tag else ""

    lines = [
        f"### 2. Cloud, GCE & Drive Sync (`{state}` | source `{src_tag}` | age `{age_txt}`)",
        f"- Colab Idle Left: `{idle}`{gpu_part} | GCE: `{gce_txt}` | Backoff Cooldown: `{cd_txt}`",
        f"- Drive: `{used_b} / {total_b}` (free `{free_b}`) | `{backend_txt}`",
    ]
    err_sum = cloud.get("error_summary") or ""
    if err_sum:
        lines.append(f"- Diagnostic: `{err_sum}`")
    return lines


def _render_mailbox_section(mailbox: Optional[dict[str, Any]]) -> list[str]:
    if not mailbox:
        return [
            "### 3. Mailbox & Background Tasks",
            "- No cached mailbox snapshot found.",
        ]

    t_cnt = int(mailbox.get("tasks_count") or 0)
    d_cnt = int(mailbox.get("done_count") or 0)
    f_cnt = int(mailbox.get("failed_count") or 0)
    u_cnt = int(mailbox.get("unverified_count") or 0)
    stale_tag = " | stale" if mailbox.get("stale") else ""

    lines = [
        f"### 3. Mailbox & Background Tasks (`tasks={t_cnt}`, `done={d_cnt}`, `failed={f_cnt}`, `unverified={u_cnt}{stale_tag}`)",
    ]
    tasks = mailbox.get("tasks") or []
    if tasks:
        task_names = ", ".join(f"`{it.get('name', '?')}`" for it in tasks[:5])
        lines.append(f"- Active Queue: {task_names}")
    done_items = mailbox.get("done_items") or []
    if done_items:
        lines.append("- Recent Done:")
        for item in done_items[:5]:
            ver = item.get("verified")
            badge = "VERIFIED" if ver is True else ("UNVERIFIED" if ver is False else "OK")
            detail = item.get("verify_detail") or ""
            detail_part = f" ({detail})" if detail else ""
            lines.append(f"  - `{item.get('name', '?')}` [{badge}] `{item.get('modified_at', '')}`{detail_part}")
    failed_items = mailbox.get("failed_items") or []
    if failed_items:
        lines.append("- Recent Failed:")
        for item in failed_items[:3]:
            tail = _clean_log_tail(str(item.get("log_tail") or ""))
            tail_part = f" | tail: `{tail}`" if tail else ""
            lines.append(f"  - `{item.get('name', '?')}` `{item.get('modified_at', '')}`{tail_part}")
    err_sum = mailbox.get("error_summary") or ""
    if err_sum:
        lines.append(f"- Mailbox Note: `{err_sum}`")
    return lines


def _render_telemetry_section(telemetry: Optional[dict[str, Any]]) -> list[str]:
    if not telemetry:
        return [
            "### 4. APM Telemetry (`telemetry.db`)",
            "- Unavailable (no telemetry database found).",
        ]
    total = int(telemetry.get("total_tasks") or 0)
    comp = int(telemetry.get("completed_tasks") or 0)
    fail = int(telemetry.get("failed_tasks") or 0)
    rate = float(telemetry.get("success_rate_percent") or 0.0)
    avg_s = float(telemetry.get("avg_duration_sec") or 0.0)
    avg_t = float(telemetry.get("avg_turns_per_task") or 0.0)
    tools_total = int(telemetry.get("total_tools_called") or 0)
    t1 = int(telemetry.get("t1_interceptions_count") or 0)
    lines = [
        "### 4. APM Telemetry (`telemetry.db`)",
        f"- Tasks: `{total}` (`{comp}` completed / `{fail}` failed | `{rate:.1f}%` success)",
        f"- Avg: `{avg_s:.2f}s` / `{avg_t:.1f}` turns | Tool Calls: `{tools_total}` | T1 Guards: `{t1}`",
    ]
    top_tools = telemetry.get("top_tools") or []
    if top_tools:
        top_str = ", ".join(f"`{t.get('tool', '?')}` ({t.get('calls', 0)})" for t in top_tools[:3])
        lines.append(f"- Top Tools: {top_str}")
    return lines


def render_inspect_markdown(payload: dict[str, Any]) -> str:
    """Format an inspect payload into a compact Markdown report for in-chat bubble output."""
    ages = payload.get("cache_ages_sec") or {}
    sections = [
        "## Faiz Control Center Diagnostic",
        "",
        *_render_quota_section(payload.get("quota"), ages.get("usage")),
        "",
        *_render_cloud_section(
            payload.get("cloud"),
            ages.get("cloud"),
            int(payload.get("backoff_remaining_sec") or 0),
        ),
        "",
        *_render_mailbox_section(payload.get("mailbox")),
        "",
        *_render_telemetry_section(payload.get("telemetry")),
    ]
    return "\n".join(sections)


def format_inspect_output(
    payload: dict[str, Any],
    *,
    as_json: bool = False,
) -> str:
    """Return either pretty JSON or Markdown bubble text for a diagnostic payload."""
    clean = sanitize_for_json(payload)
    if as_json:
        return json.dumps(clean, ensure_ascii=False, indent=2)
    return render_inspect_markdown(clean)
