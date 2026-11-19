"""Responsive layout state and persistence engine (`~/.agents/tui_layout.json`)."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Optional

from ..store import atomic_write_json

DEFAULT_LAYOUT_PATH = Path.home() / ".agents" / "tui_layout.json"
PANEL_IDS: tuple[str, ...] = ("quota", "colab", "mailbox")
PANEL_TITLES: dict[str, str] = {
    "quota": "Quota & Burn",
    "colab": "Colab & GCP",
    "mailbox": "Mailbox & Verify",
}


@dataclass
class PanelConfig:
    """Per-panel layout configuration."""

    panel_id: str
    visible: bool = True
    order: int = 0
    fr: float = 1.0


class LayoutManager:
    """Manages presets, focus order, zoom, fr weights, visibility, and JSON persistence."""

    def __init__(
        self,
        *,
        layout_path: Path = DEFAULT_LAYOUT_PATH,
        narrow_threshold_cols: int = 55,
    ) -> None:
        self.layout_path = layout_path
        self.narrow_threshold_cols = narrow_threshold_cols
        self.preset: int = 1
        self.zoomed_panel: Optional[str] = None
        self.panels: dict[str, PanelConfig] = {
            pid: PanelConfig(panel_id=pid, visible=True, order=idx, fr=1.0)
            for idx, pid in enumerate(PANEL_IDS)
        }

    def load(self) -> None:
        """Load persisted layout preferences if present and valid."""
        if not self.layout_path.exists():
            return
        try:
            data = json.loads(self.layout_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return

        preset_val = data.get("preset", 1)
        if preset_val in (1, 2):
            self.preset = int(preset_val)
        zoom_val = data.get("zoomed_panel")
        self.zoomed_panel = zoom_val if zoom_val in PANEL_IDS else None

        raw_panels = data.get("panels")
        if isinstance(raw_panels, dict):
            for idx, pid in enumerate(PANEL_IDS):
                p = raw_panels.get(pid)
                if not isinstance(p, dict):
                    continue
                fr_val = float(p.get("fr", 1.0))
                fr_val = round(max(0.3, min(3.0, fr_val)), 1)
                self.panels[pid] = PanelConfig(
                    panel_id=pid,
                    visible=bool(p.get("visible", True)),
                    order=int(p.get("order", idx)),
                    fr=fr_val,
                )
        self._normalize_orders()

    def save(self) -> None:
        """Persist current layout preferences atomically."""
        payload: dict[str, Any] = {
            "version": 1,
            "preset": self.preset,
            "auto_narrow_threshold_cols": self.narrow_threshold_cols,
            "zoomed_panel": self.zoomed_panel,
            "panels": {
                pid: {
                    "visible": cfg.visible,
                    "order": cfg.order,
                    "fr": cfg.fr,
                }
                for pid, cfg in self.panels.items()
            },
        }
        atomic_write_json(self.layout_path, payload)

    def _normalize_orders(self) -> None:
        ordered = sorted(self.panels.values(), key=lambda c: (c.order, PANEL_IDS.index(c.panel_id)))
        for idx, cfg in enumerate(ordered):
            cfg.order = idx

    def ordered_panels(self, *, visible_only: bool = False) -> list[PanelConfig]:
        """Return panels sorted by `order`."""
        items = sorted(self.panels.values(), key=lambda c: c.order)
        if visible_only:
            return [c for c in items if c.visible]
        return items

    def effective_preset(self, terminal_width: int) -> int:
        """Auto-switch to Preset 2 (vertical) when terminal width <= threshold."""
        if terminal_width > 0 and terminal_width <= self.narrow_threshold_cols:
            return 2
        return self.preset

    def set_preset(self, preset: int) -> int:
        if preset not in (1, 2):
            raise ValueError(f"Unsupported preset: {preset}")
        self.preset = preset
        self.save()
        return self.preset

    def toggle_zoom(self, panel_id: str) -> Optional[str]:
        if panel_id not in self.panels:
            return self.zoomed_panel
        self.zoomed_panel = None if self.zoomed_panel == panel_id else panel_id
        self.save()
        return self.zoomed_panel

    def adjust_fr(self, panel_id: str, delta: float) -> float:
        cfg = self.panels[panel_id]
        cfg.fr = round(max(0.3, min(3.0, cfg.fr + delta)), 1)
        self.save()
        return cfg.fr

    def toggle_visible(self, panel_id: str) -> bool:
        if panel_id not in self.panels:
            return False
        cfg = self.panels[panel_id]
        if cfg.visible:
            visible_count = sum(1 for c in self.panels.values() if c.visible)
            if visible_count <= 1:
                # Keep at least one panel visible
                return True
            cfg.visible = False
            if self.zoomed_panel == panel_id:
                self.zoomed_panel = None
        else:
            cfg.visible = True
        self.save()
        return cfg.visible

    def show_all(self) -> None:
        for cfg in self.panels.values():
            cfg.visible = True
        self.save()

    def swap_with_next(self, panel_id: str) -> Optional[str]:
        visible = self.ordered_panels(visible_only=True)
        if len(visible) < 2:
            return None
        ids = [c.panel_id for c in visible]
        if panel_id not in ids:
            return None
        idx = ids.index(panel_id)
        nxt = visible[(idx + 1) % len(visible)]
        cur = self.panels[panel_id]
        cur.order, nxt.order = nxt.order, cur.order
        self._normalize_orders()
        self.save()
        return nxt.panel_id
