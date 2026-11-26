"""Colab, GCE Spot, and Google Drive panel widget."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from rich.markup import escape
from textual.widgets import Static

from ...models import ColabGcpSnapshot
from ...store import summarize_probe_error


def _fmt_bytes(num: Optional[int]) -> str:
    if num is None:
        return "--"
    gib = num / (1024**3)
    if gib >= 1024:
        return f"{gib / 1024:.2f}T"
    return f"{gib:.1f}G"


def _fmt_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return "--"
    mins, secs = divmod(max(0, int(seconds)), 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours}h{mins:02d}m"
    return f"{mins}m{secs:02d}s"


def _fmt_uptime(created_at: str) -> str:
    raw = (created_at or "").strip()
    if not raw:
        return ""
    try:
        iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = max(0, int((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()))
        return _fmt_duration(secs)
    except ValueError:
        return ""


class ColabPanel(Static):
    """Displays Colab status.json, 60m Idle-Killer, GCE Spot, and Drive水位."""

    can_focus = True

    def __init__(self, **kwargs: object) -> None:
        super().__init__("Loading Colab & GCP...", id="panel-colab", classes="dash-panel", **kwargs)
        self.border_title = "2 Colab & GCP"

    def update_snapshot(self, snap: Optional[ColabGcpSnapshot], *, cooldown_sec: int = 0) -> None:
        if snap is None:
            self.update("[dim]Waiting for Colab / GCE / Drive probe...[/dim]")
            return

        state_style = {
            "ONLINE": "bold green",
            "STOPPED": "dim",
            "NOT_CONFIGURED": "dim",
            "STALE_OFFLINE": "bold red",
            "DEGRADED": "bold yellow",
        }.get(snap.colab_state, "cyan")

        age_str = f" ({snap.age_seconds}s)" if snap.age_seconds is not None else ""
        gpu_str = f" [{escape(snap.colab_gpu)}]" if snap.colab_gpu else ""
        mode_str = f" [{escape(snap.mode)}]" if snap.mode else ""
        cached_hint = f" (prev:{escape(snap.raw_status)})" if snap.colab_state == "DEGRADED" and snap.raw_status else ""

        idle_str = _fmt_duration(snap.idle_remaining_seconds)
        if snap.colab_state == "ONLINE" and snap.idle_remaining_seconds is not None:
            if snap.idle_remaining_seconds <= 300:
                idle_disp = f"[bold red]{idle_str} left[/bold red]"
            elif snap.idle_remaining_seconds <= 900:
                idle_disp = f"[yellow]{idle_str} left[/yellow]"
            else:
                idle_disp = f"[green]{idle_str} left[/green]"
        else:
            idle_disp = f"[dim]{idle_str}[/dim]"

        running = [inst for inst in snap.gce_instances if inst.status.upper() in {"RUNNING", "STAGING"}]
        if running:
            first = running[0]
            meta_parts: list[str] = []
            if first.machine_type:
                meta_parts.append(first.machine_type)
            if first.provisioning_model.upper() == "SPOT":
                meta_parts.append("Spot")
            up_str = _fmt_uptime(first.created_at)
            if up_str:
                meta_parts.append(up_str)
            meta_suffix = f" · {' · '.join(escape(p) for p in meta_parts)}" if meta_parts else ""
            gce_str = f"[bold red]{len(running)} RUN[/bold red] ({escape(first.name[:18])}{meta_suffix})"
        else:
            gce_str = f"[green]0 running[/green] (total {len(snap.gce_instances)})"

        cu_str = ""
        if snap.colab_balance_cu is not None:
            cu_str = f" · CU:{snap.colab_balance_cu:.1f}"

        pct_str = ""
        if snap.drive_used_bytes is not None and snap.drive_total_bytes:
            pct = (snap.drive_used_bytes / snap.drive_total_bytes) * 100.0
            pct_str = f" ({pct:.1f}%)"

        if snap.drive_backend == "gdrive":
            flag_line = "[green]gdrive: custom OAuth OK[/green]"
        else:
            flags: list[str] = []
            if snap.rclone_readonly:
                flags.append("RO")
            if snap.rclone_shared_client_id:
                flags.append("shared_id_2026")
            flag_line = (
                f"[yellow]! rclone: {' | '.join(flags)}[/yellow]"
                if flags
                else "[green]rclone: custom OAuth OK[/green]"
            )

        lines = [
            f"Colab: [{state_style}]{escape(snap.colab_state)}[/{state_style}]{gpu_str}{age_str}{mode_str}{cached_hint}",
            f"Idle: {idle_disp}{cu_str} | {escape((snap.project or '-')[:20])}",
            f"GCE: {gce_str}",
            f"Drive: {_fmt_bytes(snap.drive_used_bytes)} / {_fmt_bytes(snap.drive_total_bytes)}{pct_str}",
            flag_line,
        ]
        err_summary = summarize_probe_error(snap.error, cooldown_sec=cooldown_sec)
        if err_summary:
            lines.append(f"[yellow]! {escape(err_summary)}[/yellow]")
        elif snap.reason:
            lines.append(f"[dim]{escape(snap.reason[:44])}[/dim]")

        self.update("\n".join(lines))
