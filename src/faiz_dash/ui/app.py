"""Textual TUI application for Faiz Control Center v0.1."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.events import Resize
from textual.message import Message
from textual.widgets import Static

from ..collectors.cloud_probe import CloudProbe
from ..collectors.mailbox_probe import MailboxProbe
from ..collectors.usage_probe import UsageProbe
from ..gateways.drive import RcloneDriveGateway
from ..store import StateStore, read_backoff_remaining
from .layout import PANEL_IDS, PANEL_TITLES, LayoutManager
from .widgets import ColabPanel, MailboxPanel, QuotaPanel


class SnapshotUpdated(Message):
    """Posted when StateStore receives a new snapshot for a topic."""

    def __init__(self, topic: str) -> None:
        super().__init__()
        self.topic = topic


class FaizControlCenterApp(App[None]):
    """3-panel responsive Rex Dock / Sidebar Control Center."""

    CSS_PATH = Path(__file__).parent / "styles.tcss"

    BINDINGS = [
        Binding("1", "preset_1", "Dock", show=False),
        Binding("2", "preset_2", "Side", show=False),
        Binding("m", "zoom", "Zoom", show=False),
        Binding("[", "shrink_panel", "Fr-", show=False),
        Binding("]", "grow_panel", "Fr+", show=False),
        Binding("v", "toggle_visible", "Hide", show=False),
        Binding("0", "show_all", "ShowAll", show=False),
        Binding("!", "toggle_panel('quota')", "ToggleQuota", show=False),
        Binding("@", "toggle_panel('colab')", "ToggleColab", show=False),
        Binding("#", "toggle_panel('mailbox')", "ToggleMbox", show=False),
        Binding("s", "swap_panel", "Swap", show=False),
        Binding("u", "refresh_usage", "Usage", show=False),
        Binding("r", "refresh_cloud", "Cloud", show=False),
        Binding("q", "quit", "Quit", show=False),
    ]

    def __init__(
        self,
        *,
        dock: bool = False,
        preset: Optional[int] = None,
        store: Optional[StateStore] = None,
        layout_manager: Optional[LayoutManager] = None,
        usage_probe: Optional[UsageProbe] = None,
        cloud_probe: Optional[CloudProbe] = None,
        mailbox_probe: Optional[MailboxProbe] = None,
        enable_background_timers: bool = True,
    ) -> None:
        super().__init__()
        self.dock = dock
        self.semaphore = asyncio.Semaphore(3)
        self.store = store or StateStore()
        self.layout_manager = layout_manager or LayoutManager()
        self.layout_manager.load()
        if preset in (1, 2):
            self.layout_manager.preset = preset

        drive = RcloneDriveGateway(semaphore=self.semaphore)
        self.usage_probe = usage_probe or UsageProbe(
            cache_dir=self.store.cache_dir,
            semaphore=self.semaphore,
        )
        self.cloud_probe = cloud_probe or CloudProbe(
            drive,
            semaphore=self.semaphore,
            backoff_path=self.store.cache_dir / "cloud_backoff.json",
        )
        self.mailbox_probe = mailbox_probe or MailboxProbe(drive)
        self.enable_background_timers = enable_background_timers

        self._dirty_topics: set[str] = set()
        self._usage_lock = asyncio.Lock()
        self._cloud_lock = asyncio.Lock()
        self._bg_tasks: set[asyncio.Task[None]] = set()
        self._status_note: str = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="top-bar", markup=False)
        with Container(id="panels-container", classes="preset-horizontal"):
            yield QuotaPanel()
            yield ColabPanel()
            yield MailboxPanel()
        yield Static("", id="bottom-bar", markup=False)

    def on_mount(self) -> None:
        self.store.subscribe(self._on_store_update)
        self._apply_layout()
        self._render_all_panels()
        self._refresh_chrome_bars()

        # Focus first visible panel
        visible = self.layout_manager.ordered_panels(visible_only=True)
        if visible:
            self._panel_widget(visible[0].panel_id).focus()

        # 2Hz UI flush timer for dirty-flag coalescing
        self.set_interval(0.5, self._flush_dirty_topics)

        if self.enable_background_timers:
            self._spawn_bg(self._collect_usage(probe_pty=True, force_pty=False))
            self._spawn_bg(self._collect_cloud_and_mailbox(force=False))
            self.set_interval(3.0, self._tick_local_usage)
            self.set_interval(900.0, self._tick_pty_usage)
            self.set_interval(60.0, self._tick_cloud_and_mailbox)

    def on_unmount(self) -> None:
        self.store.unsubscribe(self._on_store_update)
        for task in list(self._bg_tasks):
            task.cancel()

    def _spawn_bg(self, coro: object) -> None:
        task = asyncio.create_task(coro)  # type: ignore[arg-type]
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _on_store_update(self, topic: str) -> None:
        self.post_message(SnapshotUpdated(topic))

    def on_snapshot_updated(self, event: SnapshotUpdated) -> None:
        self._dirty_topics.add(event.topic)

    def _cooldown_sec(self) -> int:
        return read_backoff_remaining(self.store.cache_dir / "cloud_backoff.json")

    def _flush_dirty_topics(self) -> None:
        if not self._dirty_topics:
            return
        topics = set(self._dirty_topics)
        self._dirty_topics.clear()
        cd = self._cooldown_sec()
        mbox_zoomed = self.layout_manager.zoomed_panel == "mailbox"
        if "quota" in topics:
            self._panel_widget("quota").update_snapshot(self.store.quota)  # type: ignore[attr-defined]
        if "cloud" in topics:
            self._panel_widget("colab").update_snapshot(self.store.cloud, cooldown_sec=cd)  # type: ignore[attr-defined]
        if "mailbox" in topics:
            self._panel_widget("mailbox").update_snapshot(self.store.mailbox, zoomed=mbox_zoomed, cooldown_sec=cd)  # type: ignore[attr-defined]
        self._refresh_chrome_bars()

    def _render_all_panels(self) -> None:
        cd = self._cooldown_sec()
        mbox_zoomed = self.layout_manager.zoomed_panel == "mailbox"
        self._panel_widget("quota").update_snapshot(self.store.quota)  # type: ignore[attr-defined]
        self._panel_widget("colab").update_snapshot(self.store.cloud, cooldown_sec=cd)  # type: ignore[attr-defined]
        self._panel_widget("mailbox").update_snapshot(self.store.mailbox, zoomed=mbox_zoomed, cooldown_sec=cd)  # type: ignore[attr-defined]

    def _panel_widget(self, panel_id: str) -> Static:
        return self.query_one(f"#panel-{panel_id}", Static)

    def _focused_panel_id(self) -> str:
        focused = self.focused
        if focused is not None and focused.id and focused.id.startswith("panel-"):
            pid = focused.id.removeprefix("panel-")
            if pid in PANEL_IDS:
                return pid
        visible = self.layout_manager.ordered_panels(visible_only=True)
        return visible[0].panel_id if visible else "quota"

    def _apply_layout(self) -> None:
        container = self.query_one("#panels-container", Container)
        eff_preset = self.layout_manager.effective_preset(self.size.width)
        if eff_preset == 2:
            container.remove_class("preset-horizontal")
            container.add_class("preset-vertical")
        else:
            container.remove_class("preset-vertical")
            container.add_class("preset-horizontal")

        ordered = self.layout_manager.ordered_panels(visible_only=False)
        prev_widget: Optional[Static] = None
        for idx, cfg in enumerate(ordered):
            widget = self._panel_widget(cfg.panel_id)
            if idx == 0:
                container.move_child(widget, before=0)
            elif prev_widget is not None:
                container.move_child(widget, after=prev_widget)
            prev_widget = widget

            zoomed = self.layout_manager.zoomed_panel
            if zoomed is not None:
                is_zoom_target = cfg.panel_id == zoomed
                widget.display = is_zoom_target
                widget.styles.width = "100%"
                widget.styles.height = "100%"
                widget.border_title = f"{idx + 1} {PANEL_TITLES[cfg.panel_id]} [ZOOM]"
            else:
                widget.display = cfg.visible
                if eff_preset == 1:
                    widget.styles.width = f"{cfg.fr}fr"
                    widget.styles.height = "100%"
                else:
                    widget.styles.width = "100%"
                    widget.styles.height = f"{cfg.fr}fr"
                widget.border_title = f"{idx + 1} {PANEL_TITLES[cfg.panel_id]} ({cfg.fr:.1f}fr)"

        self._panel_widget("mailbox").update_snapshot(  # type: ignore[attr-defined]
            self.store.mailbox,
            zoomed=(self.layout_manager.zoomed_panel == "mailbox"),
            cooldown_sec=self._cooldown_sec(),
        )
        self._refresh_chrome_bars()

    def _refresh_chrome_bars(self) -> None:
        eff_preset = self.layout_manager.effective_preset(self.size.width)
        gate = self.store.quota.quota_gate if self.store.quota else "--"
        colab = self.store.cloud.colab_state if self.store.cloud else "--"
        mbox_fail = self.store.mailbox.failed_count if self.store.mailbox else 0

        vis_tags = " ".join(
            f"[{'*' if self.layout_manager.panels[pid].visible else ' '}{i + 1}:{pid[:3].upper()}]"
            for i, pid in enumerate(PANEL_IDS)
        )
        note = f" | {self._status_note}" if self._status_note else ""
        top_text = (
            f"FAIZ DASH v0.1 | P{eff_preset} | Gate:{gate} | CLB:{colab} | Fail:{mbox_fail}"
            if self.size.width <= 60
            else f"FAIZ CONTROL CENTER v0.1 | Preset:{eff_preset} | Gate:{gate} | Colab:{colab} | Fail:{mbox_fail} | {vis_tags}{note}"
        )
        self.query_one("#top-bar", Static).update(top_text)

        footer = (
            "Tab 1/2 m []:Fr v(0) s u r q"
            if self.size.width <= 60
            else "Tab:Focus  1:Dock  2:Side  m:Zoom  [/]:Fr  v:Hide(0:All)  s:Swap  u:/usage  r:Cloud  q:Quit"
        )
        self.query_one("#bottom-bar", Static).update(footer)

    def on_resize(self, event: Resize) -> None:
        self._apply_layout()

    async def _collect_usage(self, *, probe_pty: bool, force_pty: bool) -> None:
        if self._usage_lock.locked():
            return
        async with self._usage_lock:
            try:
                snap = await self.usage_probe.collect(probe_pty=probe_pty, force_pty=force_pty)
                await self.store.publish_quota(snap)
            finally:
                if self._status_note == "Refreshing /usage...":
                    self._status_note = ""
                    self._refresh_chrome_bars()

    async def _collect_cloud_and_mailbox(self, *, force: bool) -> None:
        if self._cloud_lock.locked():
            return
        async with self._cloud_lock:
            try:
                cloud_snap = await self.cloud_probe.collect(force=force)
                await self.store.publish_cloud(cloud_snap)
                skip_mbox = (not force) and (
                    self.cloud_probe.in_backoff_skip
                    or ("RATE_LIMIT" in (cloud_snap.error or "").upper())
                )
                mbox_snap = await self.mailbox_probe.collect(
                    default_project=cloud_snap.project,
                    colab_online=(cloud_snap.colab_state == "ONLINE"),
                    webui_url=cloud_snap.webui_url,
                    skip_drive=skip_mbox,
                    skip_error=cloud_snap.error,
                )
                await self.store.publish_mailbox(mbox_snap)
            finally:
                if self._status_note == "Refreshing cloud...":
                    self._status_note = ""
                    self._refresh_chrome_bars()

    def _tick_local_usage(self) -> None:
        if not self.layout_manager.panels["quota"].visible and self.layout_manager.zoomed_panel != "quota":
            return
        self._spawn_bg(self._collect_usage(probe_pty=False, force_pty=False))

    def _tick_pty_usage(self) -> None:
        self._spawn_bg(self._collect_usage(probe_pty=True, force_pty=False))

    def _tick_cloud_and_mailbox(self) -> None:
        self._spawn_bg(self._collect_cloud_and_mailbox(force=False))

    def action_preset_1(self) -> None:
        self.layout_manager.set_preset(1)
        self._apply_layout()

    def action_preset_2(self) -> None:
        self.layout_manager.set_preset(2)
        self._apply_layout()

    def action_zoom(self) -> None:
        pid = self._focused_panel_id()
        self.layout_manager.toggle_zoom(pid)
        self._apply_layout()

    def action_shrink_panel(self) -> None:
        pid = self._focused_panel_id()
        self.layout_manager.adjust_fr(pid, -0.1)
        self._apply_layout()

    def action_grow_panel(self) -> None:
        pid = self._focused_panel_id()
        self.layout_manager.adjust_fr(pid, 0.1)
        self._apply_layout()

    def action_toggle_visible(self) -> None:
        pid = self._focused_panel_id()
        self.layout_manager.toggle_visible(pid)
        self._apply_layout()
        visible = self.layout_manager.ordered_panels(visible_only=True)
        if visible and not self.layout_manager.panels[pid].visible:
            self._panel_widget(visible[0].panel_id).focus()

    def action_toggle_panel(self, panel_id: str) -> None:
        if panel_id in PANEL_IDS:
            self.layout_manager.toggle_visible(panel_id)
            self._apply_layout()

    def action_show_all(self) -> None:
        self.layout_manager.show_all()
        self._apply_layout()

    def action_swap_panel(self) -> None:
        pid = self._focused_panel_id()
        self.layout_manager.swap_with_next(pid)
        self._apply_layout()

    def action_refresh_usage(self) -> None:
        self._status_note = "Refreshing /usage..."
        self._refresh_chrome_bars()
        self._spawn_bg(self._collect_usage(probe_pty=True, force_pty=True))

    def action_refresh_cloud(self) -> None:
        self._status_note = "Refreshing cloud..."
        self._refresh_chrome_bars()
        self._spawn_bg(self._collect_cloud_and_mailbox(force=True))
