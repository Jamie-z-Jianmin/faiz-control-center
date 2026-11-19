"""Mailbox & Anti-False-Done verification panel widget."""

from __future__ import annotations

from typing import Optional

from rich.markup import escape
from textual.widgets import Static

from ...models import MailboxSnapshot
from ...store import summarize_probe_error


class MailboxPanel(Static):
    """Displays mailbox/{tasks,done,failed} depth, log tails, and output verification."""

    can_focus = True

    def __init__(self, **kwargs: object) -> None:
        super().__init__("Loading Mailbox...", id="panel-mailbox", classes="dash-panel", **kwargs)
        self.border_title = "3 Mailbox & Verify"

    def update_snapshot(
        self,
        snap: Optional[MailboxSnapshot],
        *,
        zoomed: bool = False,
        cooldown_sec: int = 0,
    ) -> None:
        if snap is None:
            self.update("[dim]Waiting for Drive Mailbox scan...[/dim]")
            return

        fail_badge = (
            f"[bold red]{snap.failed_count}[/bold red]"
            if snap.failed_count > 0
            else f"[green]{snap.failed_count}[/green]"
        )
        lines = [
            f"Q: tasks:[bold]{snap.tasks_count}[/bold]  done:[green]{snap.done_count}[/green]  fail:{fail_badge}",
        ]

        fail_limit = 5 if zoomed else 2
        for fitem in snap.failed_items[:fail_limit]:
            fname = fitem.name if zoomed else fitem.name[:36]
            ts_tag = f" [dim]{escape(fitem.modified_at[-8:])}[/dim]" if zoomed and fitem.modified_at else ""
            lines.append(f"[red]✗ {escape(fname)}[/red]{ts_tag}")
            if fitem.log_tail:
                raw_tail = summarize_probe_error(fitem.log_tail) if "NOTICE:" in fitem.log_tail else fitem.log_tail
                tail_str = raw_tail if zoomed else raw_tail[:44]
                lines.append(f"[dim]  {escape(tail_str)}[/dim]")

        task_limit = 3 if zoomed else 2
        for titem in snap.tasks[:task_limit]:
            tname = titem.name if zoomed else titem.name[:36]
            lines.append(f"[cyan]▸ {escape(tname)}[/cyan]")

        done_limit = 4 if zoomed else 2
        for item in snap.done_items[:done_limit]:
            if item.verified is True:
                icon = "[green]✓[/green]"
            elif item.verified is False:
                icon = "[bold red]![/bold red]"
            else:
                icon = "[dim]?[/dim]"
            short_name = item.name.removesuffix(".tts.json").removesuffix(".json")
            detail = escape(item.verify_detail[:36] if zoomed else item.verify_detail[:22])
            lines.append(f"{icon} {escape(short_name[:28 if zoomed else 20])} [dim]{detail}[/dim]")

        if not snap.failed_items and not snap.done_items and not snap.tasks:
            lines.append("[dim]Mailbox empty[/dim]")

        err_summary = summarize_probe_error(snap.error, cooldown_sec=cooldown_sec)
        if err_summary:
            lines.append(f"[yellow]! {escape(err_summary)}[/yellow]")

        self.update("\n".join(lines))
