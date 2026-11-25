"""Quota & Burn-Rate panel widget."""

from __future__ import annotations

from typing import Optional

from rich.markup import escape
from textual.widgets import Static

from ...models import ModelBucketQuota, QuotaSnapshot
from ...store import (
    WINDOW_5H_SECONDS,
    WINDOW_WEEKLY_SECONDS,
    compute_quota_pace,
    remaining_reset_seconds,
    summarize_probe_error,
)


def _fmt_tokens(val: int) -> str:
    if val >= 1_000_000:
        return f"{val / 1_000_000:.2f}M"
    if val >= 1_000:
        return f"{val / 1_000:.1f}k"
    return str(val)


def _mini_bar(pct: Optional[float], width: int = 8) -> str:
    if pct is None:
        return "[dim]" + ("?" * width) + "[/dim]"
    clamped = max(0.0, min(100.0, pct))
    filled = int(round((clamped / 100.0) * width))
    empty = width - filled
    color = "green" if clamped >= 40 else ("yellow" if clamped >= 20 else "red")
    return f"[{color}]{'█' * filled}[/{color}][dim]{'░' * empty}[/dim]"


def _pct_text(pct: Optional[float]) -> str:
    if pct is None:
        return "--%"
    color = "green" if pct >= 40 else ("yellow" if pct >= 20 else "bold red")
    return f"[{color}]{pct:.1f}%[/{color}]"


def _short_refresh(text: str) -> str:
    if not text:
        return ""
    if text.lower().startswith("refreshes in "):
        return text[13:].replace(" ", "")
    if "available" in text.lower():
        return "avail"
    return text[:10]


def _fmt_pace_dur(seconds: int) -> str:
    total = max(0, int(seconds))
    if total >= 86400:
        days, rem = divmod(total, 86400)
        return f"{days}d{rem // 3600}h"
    hours, rem = divmod(total, 3600)
    mins = rem // 60
    if hours > 0:
        return f"{hours}h{mins:02d}m"
    return f"{max(1, mins)}m"


def _pace_markup(label: str, pace: Optional[dict[str, object]]) -> str:
    if not pace:
        return ""
    delta = int(pace.get("pace_delta_pct") or 0)  # type: ignore[arg-type]
    status = str(pace.get("status") or "on_pace")
    ro = pace.get("runs_out_seconds")
    if ro is not None and delta >= 20:
        color = "bold red"
    elif status == "over":
        color = "yellow"
    elif status == "under":
        color = "green"
    else:
        color = "dim"
    sign_txt = f"{delta:+d}%" if delta != 0 else "0%"
    ro_txt = f" (~{_fmt_pace_dur(int(ro))})" if ro is not None else ""  # type: ignore[arg-type]
    return f"{label} [{color}]{sign_txt}{ro_txt}[/{color}]"


def _find_bucket(buckets: tuple[ModelBucketQuota, ...], keyword: str) -> Optional[ModelBucketQuota]:
    for b in buckets:
        if keyword in b.group_name.upper():
            return b
    return None


class QuotaPanel(Static):
    """Displays `/usage` buckets, Pro vs GCP channels, and transcript burn-rate."""

    can_focus = True

    def __init__(self, **kwargs: object) -> None:
        super().__init__("Loading quota...", id="panel-quota", classes="dash-panel", **kwargs)
        self.border_title = "1 Quota & Burn"

    def update_snapshot(self, snap: Optional[QuotaSnapshot]) -> None:
        if snap is None:
            self.update("[dim]Waiting for /usage & transcript burn...[/dim]")
            return

        gem = _find_bucket(snap.buckets, "GEMINI MODELS")
        cla = _find_bucket(snap.buckets, "CLAUDE")
        arg = _find_bucket(snap.buckets, "ARGON")

        pro_5h = gem.five_hour_pct if gem else None
        pro_5h_ref = _short_refresh(gem.five_hour_refresh) if gem else ""
        pro_wk = gem.weekly_pct if gem else None
        cla_5h = cla.five_hour_pct if cla else None
        cla_wk = cla.weekly_pct if cla else None
        cla_ref = _short_refresh(cla.weekly_refresh) if cla else ""
        arg_wk = arg.weekly_pct if arg else None
        arg_ref = _short_refresh(arg.weekly_refresh) if arg else ""

        gate_color = {
            "GREEN": "green",
            "THROTTLE_ARGON": "yellow",
            "WARN_PRO": "bold red",
        }.get(snap.quota_gate, "cyan")

        if arg is not None:
            line2 = (
                f"Argon  {_mini_bar(arg_wk)} {_pct_text(arg_wk)}"
                + (f" [dim]{escape(arg_ref)}[/dim]" if arg_ref else "")
                + f" | C/G {_pct_text(cla_wk)}"
            )
        else:
            line2 = (
                f"Claude {_mini_bar(cla_wk)} {_pct_text(cla_wk)}"
                + (f" [dim]{escape(cla_ref)}[/dim]" if cla_ref else "")
                + f" | 5h {_pct_text(cla_5h)}"
            )

        lines = [
            f"Pro 5h {_mini_bar(pro_5h)} {_pct_text(pro_5h)}"
            + (f" [dim]{escape(pro_5h_ref)}[/dim]" if pro_5h_ref else "")
            + f" | Wk {_pct_text(pro_wk)}",
            line2,
        ]
        if gem is not None:
            p5 = compute_quota_pace(
                pro_5h,
                remaining_reset_seconds(gem.five_hour_refresh, snap.pty_fetched_epoch),
                WINDOW_5H_SECONDS,
            )
            pwk = compute_quota_pace(
                pro_wk,
                remaining_reset_seconds(gem.weekly_refresh, snap.pty_fetched_epoch),
                WINDOW_WEEKLY_SECONDS,
            )
            pace_parts = [p for p in (_pace_markup("5h", p5), _pace_markup("Wk", pwk)) if p]
            if pace_parts:
                lines.append("Pace: " + " | ".join(pace_parts))
        lines.extend(
            [
                f"Chan: Pro:[bold]{snap.channels.pro_count}[/bold] GCP:[bold]{snap.channels.gcp_count}[/bold]"
                f" | Gate:[{gate_color}]{escape(snap.quota_gate)}[/{gate_color}]",
                f"Global Burn: 1h [cyan]{_fmt_tokens(snap.burn.tokens_1h)}[/cyan]"
                f" | 24h [cyan]{_fmt_tokens(snap.burn.tokens_24h)}[/cyan]"
                f" | Cache [bold]{snap.burn.cache_hit_rate * 100:.0f}%[/bold]",
            ]
        )

        top_models = ", ".join(
            f"{escape(m.replace('gemini-', 'g'))}:{_fmt_tokens(t)}"
            for m, t in list(snap.burn.by_model_24h.items())[:2]
        )
        acct_str = escape(snap.account) if snap.account else snap.channels.active_auth_method or "pro"
        lines.append(
            f"Act:{snap.burn.active_sessions} | {top_models or acct_str}"
        )
        if snap.error:
            lines.append(f"[yellow]! {escape(summarize_probe_error(snap.error))}[/yellow]")
        elif snap.channels.quota_project:
            lines.append(f"[dim]ADC:{escape(snap.channels.quota_project[:24])} ({snap.source})[/dim]")
        else:
            lines.append(f"[dim]{acct_str} ({snap.source})[/dim]")

        self.update("\n".join(lines))
