"""Interactive bilingual (en/zh) Textual settings screen for Faiz statusLine (`faiz-dash config`)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, Footer, Header, Label, Select, Static

from ..statusline_config import (
    CAPSULE_COLOR_KEYS,
    COLOR_SLOTS,
    DEFAULT_DOCK_RATIO,
    DEFAULT_SIDE_RATIO,
    DEFAULT_STATUSLINE_CONFIG,
    DEFAULT_STATUSLINE_CONFIG_PATH,
    MODULE_KEYS,
    VALID_CAPSULE_COLOR_TOKENS,
    VALID_COLORS,
    VALID_DOCK_RATIOS,
    VALID_MAX_WIDTHS,
    VALID_ROW_COLOR_TOKENS,
    VALID_SIDE_RATIOS,
    load_statusline_config,
    read_auto_dock_enabled,
    save_statusline_config,
    write_auto_dock_enabled,
)

def _resolve_statusline_script() -> Path:
    env_override = os.environ.get("FAIZ_STATUSLINE_SCRIPT")
    if env_override:
        return Path(env_override)
    installed = Path.home() / ".agents" / "scripts" / "statusline.sh"
    if installed.exists():
        return installed
    repo_copy = Path(__file__).resolve().parents[3] / "scripts" / "statusline.sh"
    return repo_copy if repo_copy.exists() else installed


STATUSLINE_SCRIPT = _resolve_statusline_script()

I18N: dict[str, dict[str, Any]] = {
    "en": {
        "title": "Faiz StatusLine Settings (faiz-dash config)",
        "sec_modules": "1. Modules & Wrapping",
        "sec_control": "3. Control Center & Rex Panes",
        "sec_colors": "2. Visual Style, Theme & Colors",
        "auto_wrap": "Auto-wrap by terminal width (Auto-Wrap)",
        "top_gap": "Top gap below agy model header (Top Gap)",
        "line_gap": "Insert blank spacer between rows (Line Gap)",
        "auto_dock": "Auto-open bottom Dock on startup (faiz-dash on/off)",
        "max_width_label": "StatusLine Max Width:",
        "side_ratio_label": "Rex Sidebar Width:",
        "dock_ratio_label": "Rex Dock Height:",
        "lang_label": "Language:",
        "style_label": "Capsule Style:",
        "indicator_label": "Indicator:",
        "density_label": "Density:",
        "secondary_label": "Row 2 Col 2:",
        "theme_label": "Color Theme:",
        "color_mode_label": "Color Mode:",
        "quota_prec_label": "Quota Precision:",
        "row_labels": ("Row 1 Color:", "Row 2 Color:", "Row 3 Color:"),
        "sec_capsule_colors": "Per-Capsule Colors (Optional Override)",
        "btn_save": "Save (S)",
        "btn_reset": "Reset Defaults",
        "btn_quit": "Quit (Q)",
        "preview_hdr": "Live TrueColor Preview:",
        "saved_msg": "Saved to {path}",
        "modules": {
            "context": "Context Window %",
            "cache": "Prompt Cache Hit & TTL",
            "speed": "Live Token Rate (t/s)",
            "quota": "Model Quotas & Gate",
            "sessions": "Active Pro & GCP Sessions",
            "colab": "Colab & GCE Runtime",
            "mailbox": "Mailbox Task Queue",
            "freshness": "Sync Freshness Tag",
        },
        "colors": {
            "context": "Context Accent",
            "ok": "Healthy / Online",
            "warn": "Warning / Throttled",
            "alert": "Error / Failed",
            "muted": "Muted / Idle",
        },
        "capsule_colors": {
            "context": "Context Pill Color",
            "cache": "Cache Pill Color",
            "speed": "Speed Pill Color",
            "gemini": "Gemini Quota Color",
            "argon": "Claude Quota Color",
            "sessions": "Session / Burn Color",
            "colab": "Colab / GCE Color",
            "mailbox": "Queue / Mailbox Color",
            "freshness": "Freshness Tag Color",
        },
    },
    "zh": {
        "title": "Faiz StatusLine 自定义设置 (faiz-dash config)",
        "sec_modules": "1. 启用模块与自动换行",
        "sec_control": "3. 控制中心与 Rex 分屏",
        "sec_colors": "2. 胶囊风格、主题与自定义配色",
        "auto_wrap": "终端窄屏自动隔行 (Auto-Wrap)",
        "top_gap": "系统模型栏下方留空分隔 (Top Gap)",
        "line_gap": "多行胶囊间插入空行呼吸间距 (Line Gap)",
        "auto_dock": "新会话启动自动弹出底部 Dock (on/off)",
        "max_width_label": "状态栏最大列宽:",
        "side_ratio_label": "Rex 右侧边栏宽度:",
        "dock_ratio_label": "Rex 底部 Dock 高度:",
        "lang_label": "界面与胶囊语言:",
        "style_label": "胶囊视觉风格:",
        "indicator_label": "进度指示图形:",
        "density_label": "排列密度:",
        "secondary_label": "第2行第2栏:",
        "theme_label": "主题色盘:",
        "color_mode_label": "上色模式:",
        "quota_prec_label": "配额显示精度:",
        "row_labels": ("第 1 行颜色:", "第 2 行颜色:", "第 3 行颜色:"),
        "sec_capsule_colors": "独立胶囊配色 (按需覆盖统一底座)",
        "btn_save": "保存生效 (S)",
        "btn_reset": "恢复默认",
        "btn_quit": "退出 (Q)",
        "preview_hdr": "实时 TrueColor 预览:",
        "saved_msg": "已保存至 {path}",
        "modules": {
            "context": "上下文窗口 / Context %",
            "cache": "缓存命中与冷却 / Cache",
            "speed": "实时生成速率 / Token Rate",
            "quota": "模型配额与门禁 / Quota & Gate",
            "sessions": "活跃通道进程 / Pro & GCP",
            "colab": "Colab 与 GCE / Cloud",
            "mailbox": "任务队列 / Mailbox Queue",
            "freshness": "同步状态尾标 / Freshness",
        },
        "colors": {
            "context": "上下文主色 (Context)",
            "ok": "正常/在线色 (OK)",
            "warn": "预警/限流色 (Warn)",
            "alert": "失败/失联色 (Alert)",
            "muted": "次要/停机色 (Muted)",
        },
        "capsule_colors": {
            "context": "上下文胶囊底色",
            "cache": "缓存胶囊底色",
            "speed": "速率胶囊底色",
            "gemini": "Gemini 配额底色",
            "argon": "Claude 配额底色",
            "sessions": "消耗 / 会话底色",
            "colab": "Colab / GCE 底色",
            "mailbox": "任务队列底色",
            "freshness": "同步尾标底色",
        },
    },
}


def _int_select_options(
    presets: tuple[int, ...],
    current: int,
    *,
    zero_label: Optional[str] = None,
    suffix: str = "",
) -> list[tuple[str, int]]:
    values = list(presets)
    if current not in values:
        values.append(current)
        values.sort()
    opts: list[tuple[str, int]] = []
    for v in values:
        if v == 0 and zero_label is not None:
            opts.append((zero_label, v))
        else:
            opts.append((f"{v}{suffix}", v))
    return opts


class StatuslineConfigApp(App[None]):
    """Interactive terminal config UI for statusLine style, theme, modules, language, and colors."""

    CSS = """
    Screen {
        background: #141820;
        color: #d8dee9;
    }
    #main-row {
        height: 1fr;
        padding: 1 2;
    }
    .col-box {
        width: 1fr;
        border: round #4c566a;
        padding: 1;
        margin-right: 1;
        height: 100%;
        overflow-y: auto;
    }
    .sec-title {
        text-style: bold;
        color: #88c0d0;
        margin-bottom: 1;
    }
    .color-row {
        height: 3;
        align-vertical: middle;
    }
    .color-label {
        width: 24;
    }
    Select {
        width: 26;
    }
    #preview-box {
        border: round #88c0d0;
        padding: 1 2;
        margin: 0 2 1 2;
        height: auto;
    }
    #action-row {
        height: 3;
        padding: 0 2;
        align-horizontal: left;
    }
    Button {
        margin-right: 2;
    }
    """

    BINDINGS = [
        Binding("s", "save_config", "Save"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        config_path: Optional[Path] = None,
        disabled_flag_path: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self.config_path = config_path or DEFAULT_STATUSLINE_CONFIG_PATH
        self.disabled_flag_path = disabled_flag_path
        self.cfg: dict[str, Any] = load_statusline_config(self.config_path)
        self.auto_dock: bool = read_auto_dock_enabled(self.disabled_flag_path)

    def _tr(self, lang: Optional[str] = None) -> dict[str, Any]:
        code = lang or str(self.cfg.get("language", "en"))
        return I18N.get(code, I18N["en"])

    def compose(self) -> ComposeResult:
        t = self._tr()
        yield Header(show_clock=False)
        with Horizontal(id="main-row"):
            with Vertical(classes="col-box"):
                yield Static(t["sec_modules"], id="sec-modules", classes="sec-title")
                for key in MODULE_KEYS:
                    yield Checkbox(
                        t["modules"][key],
                        value=bool(self.cfg["modules"].get(key, True)),
                        id=f"mod-{key}",
                    )
                yield Checkbox(
                    t["auto_wrap"],
                    value=bool(self.cfg.get("auto_wrap", True)),
                    id="opt-autowrap",
                )
                yield Checkbox(
                    t["top_gap"],
                    value=bool(self.cfg.get("top_gap", True)),
                    id="opt-topgap",
                )
                yield Checkbox(
                    t["line_gap"],
                    value=bool(self.cfg.get("line_gap", False)),
                    id="opt-linegap",
                )
                cc_cfg = self.cfg.get("control_center") or {}
                cur_max_w = int(self.cfg.get("max_width", 0))
                cur_side_r = int(cc_cfg.get("side_ratio", DEFAULT_SIDE_RATIO))
                cur_dock_r = int(cc_cfg.get("dock_ratio", DEFAULT_DOCK_RATIO))
                yield Static(t["sec_control"], id="sec-control", classes="sec-title")
                yield Checkbox(
                    t["auto_dock"],
                    value=self.auto_dock,
                    id="opt-autodock",
                )
                with Horizontal(classes="color-row"):
                    yield Label(t["max_width_label"], id="lbl-maxwidth", classes="color-label")
                    yield Select(
                        _int_select_options(VALID_MAX_WIDTHS, cur_max_w, zero_label="Auto (0)", suffix=" cols"),
                        value=cur_max_w,
                        id="opt-maxwidth",
                        allow_blank=False,
                    )
                with Horizontal(classes="color-row"):
                    yield Label(t["side_ratio_label"], id="lbl-sideratio", classes="color-label")
                    yield Select(
                        _int_select_options(VALID_SIDE_RATIOS, cur_side_r, suffix="%"),
                        value=cur_side_r,
                        id="opt-sideratio",
                        allow_blank=False,
                    )
                with Horizontal(classes="color-row"):
                    yield Label(t["dock_ratio_label"], id="lbl-dockratio", classes="color-label")
                    yield Select(
                        _int_select_options(VALID_DOCK_RATIOS, cur_dock_r, suffix="%"),
                        value=cur_dock_r,
                        id="opt-dockratio",
                        allow_blank=False,
                    )
            with Vertical(classes="col-box"):
                yield Static(t["sec_colors"], id="sec-colors", classes="sec-title")
                with Horizontal(classes="color-row"):
                    yield Label(t["lang_label"], id="lbl-lang", classes="color-label")
                    yield Select(
                        [("English (en)", "en"), ("中文 (zh)", "zh")],
                        value=self.cfg.get("language", "en"),
                        id="opt-lang",
                        allow_blank=False,
                    )
                with Horizontal(classes="color-row"):
                    yield Label(t["style_label"], id="lbl-style", classes="color-label")
                    yield Select(
                        [
                            ("Capsule Frosted (Default)", "capsule"),
                            ("Ghost Transparent (agy-capsule)", "ghost"),
                            ("Powerline Solid (Bold)", "powerline"),
                            ("Pill Accent (Left Bar)", "pill"),
                            ("Minimal Text", "minimal"),
                        ],
                        value=self.cfg.get("style", "capsule"),
                        id="opt-style",
                        allow_blank=False,
                    )
                with Horizontal(classes="color-row"):
                    yield Label(t["indicator_label"], id="lbl-indicator", classes="color-label")
                    yield Select(
                        [
                            ("Ring (○◔◑◕●)", "ring"),
                            ("Bar (■□ 6-cell)", "bar"),
                        ],
                        value=self.cfg.get("indicator", "ring"),
                        id="opt-indicator",
                        allow_blank=False,
                    )
                with Horizontal(classes="color-row"):
                    yield Label(t["density_label"], id="lbl-density", classes="color-label")
                    yield Select(
                        [
                            ("Auto (4x2 Narrow / 2x4 Wide)", "auto"),
                            ("Compact (Dense 1-Row)", "compact"),
                            ("Expanded (All Pills)", "expanded"),
                        ],
                        value=self.cfg.get("density", "auto"),
                        id="opt-density",
                        allow_blank=False,
                    )
                with Horizontal(classes="color-row"):
                    yield Label(t["secondary_label"], id="lbl-secondary", classes="color-label")
                    yield Select(
                        [
                            ("1h Token Burn (Default)", "burn"),
                            ("5h Quota Reset", "reset"),
                            ("CLI Sessions", "sessions"),
                        ],
                        value=self.cfg.get("secondary_metric", "burn"),
                        id="opt-secondary",
                        allow_blank=False,
                    )
                with Horizontal(classes="color-row"):
                    yield Label(t["theme_label"], id="lbl-theme", classes="color-label")
                    yield Select(
                        [
                            ("Google Brand (4-Color)", "google"),
                            ("Nord Slate", "nord"),
                            ("Tokyo Night", "tokyo_night"),
                            ("Catppuccin Mocha", "catppuccin"),
                            ("Custom Slots", "custom"),
                        ],
                        value=self.cfg.get("theme", "google"),
                        id="opt-theme",
                        allow_blank=False,
                    )
                with Horizontal(classes="color-row"):
                    yield Label(t["color_mode_label"], id="lbl-colormode", classes="color-label")
                    yield Select(
                        [
                            ("Per-Row Uniform Color", "row"),
                            ("Semantic Status Color", "semantic"),
                        ],
                        value=self.cfg.get("color_mode", "row"),
                        id="opt-colormode",
                        allow_blank=False,
                    )
                with Horizontal(classes="color-row"):
                    yield Label(t["quota_prec_label"], id="lbl-quotaprec", classes="color-label")
                    yield Select(
                        [
                            ("Exact 2dp (21.36%)", "exact"),
                            ("1 Decimal (21.4%)", "1dp"),
                            ("Integer (21%)", "int"),
                        ],
                        value=self.cfg.get("quota_precision", "exact"),
                        id="opt-quotaprec",
                        allow_blank=False,
                    )
                row_opts = [(c.title(), c) for c in VALID_ROW_COLOR_TOKENS]
                cur_rows = list(self.cfg.get("row_colors") or DEFAULT_STATUSLINE_CONFIG["row_colors"])
                for idx in range(3):
                    default_tok = cur_rows[idx] if idx < len(cur_rows) else VALID_ROW_COLOR_TOKENS[idx]
                    with Horizontal(classes="color-row"):
                        yield Label(t["row_labels"][idx], id=f"lbl-rowcol-{idx}", classes="color-label")
                        yield Select(
                            row_opts,
                            value=default_tok,
                            id=f"rowcol-{idx}",
                            allow_blank=False,
                        )
                color_opts = [(c, c) for c in VALID_COLORS]
                for slot in COLOR_SLOTS:
                    with Horizontal(classes="color-row"):
                        yield Label(t["colors"][slot], id=f"lbl-col-{slot}", classes="color-label")
                        yield Select(
                            color_opts,
                            value=self.cfg["colors"].get(slot, "cyan"),
                            id=f"col-{slot}",
                            allow_blank=False,
                        )
                yield Static(t["sec_capsule_colors"], id="sec-capcolors", classes="sec-title")
                cap_opts = [
                    ("Auto (Unified)", "auto") if tok == "auto" else (tok.title(), tok)
                    for tok in VALID_CAPSULE_COLOR_TOKENS
                ]
                cur_cap_cols = self.cfg.get("capsule_colors") or {}
                for cap_key in CAPSULE_COLOR_KEYS:
                    with Horizontal(classes="color-row"):
                        yield Label(t["capsule_colors"][cap_key], id=f"lbl-capcol-{cap_key}", classes="color-label")
                        yield Select(
                            cap_opts,
                            value=cur_cap_cols.get(cap_key, "auto"),
                            id=f"capcol-{cap_key}",
                            allow_blank=False,
                        )
        yield Static("", id="preview-box")
        with Horizontal(id="action-row"):
            yield Button(t["btn_save"], variant="primary", id="btn-save")
            yield Button(t["btn_reset"], variant="warning", id="btn-reset")
            yield Button(t["btn_quit"], id="btn-quit")
        yield Footer()

    def on_mount(self) -> None:
        self._apply_i18n(str(self.cfg.get("language", "en")))
        self._update_preview()

    def _apply_i18n(self, lang: str) -> None:
        t = self._tr(lang)
        self.title = t["title"]
        self.query_one("#sec-modules", Static).update(t["sec_modules"])
        self.query_one("#sec-control", Static).update(t["sec_control"])
        self.query_one("#sec-colors", Static).update(t["sec_colors"])
        self.query_one("#lbl-maxwidth", Label).update(t["max_width_label"])
        self.query_one("#lbl-sideratio", Label).update(t["side_ratio_label"])
        self.query_one("#lbl-dockratio", Label).update(t["dock_ratio_label"])
        self.query_one("#lbl-lang", Label).update(t["lang_label"])
        self.query_one("#lbl-style", Label).update(t["style_label"])
        self.query_one("#lbl-indicator", Label).update(t["indicator_label"])
        self.query_one("#lbl-density", Label).update(t["density_label"])
        self.query_one("#lbl-secondary", Label).update(t["secondary_label"])
        self.query_one("#lbl-theme", Label).update(t["theme_label"])
        self.query_one("#lbl-colormode", Label).update(t["color_mode_label"])
        self.query_one("#lbl-quotaprec", Label).update(t["quota_prec_label"])
        for idx in range(3):
            self.query_one(f"#lbl-rowcol-{idx}", Label).update(t["row_labels"][idx])
        for key in MODULE_KEYS:
            self.query_one(f"#mod-{key}", Checkbox).label = t["modules"][key]  # type: ignore[assignment]
        self.query_one("#opt-autowrap", Checkbox).label = t["auto_wrap"]  # type: ignore[assignment]
        self.query_one("#opt-topgap", Checkbox).label = t["top_gap"]  # type: ignore[assignment]
        self.query_one("#opt-linegap", Checkbox).label = t["line_gap"]  # type: ignore[assignment]
        self.query_one("#opt-autodock", Checkbox).label = t["auto_dock"]  # type: ignore[assignment]
        for slot in COLOR_SLOTS:
            self.query_one(f"#lbl-col-{slot}", Label).update(t["colors"][slot])
        self.query_one("#sec-capcolors", Static).update(t["sec_capsule_colors"])
        for cap_key in CAPSULE_COLOR_KEYS:
            self.query_one(f"#lbl-capcol-{cap_key}", Label).update(t["capsule_colors"][cap_key])
        self.query_one("#btn-save", Button).label = t["btn_save"]
        self.query_one("#btn-reset", Button).label = t["btn_reset"]
        self.query_one("#btn-quit", Button).label = t["btn_quit"]

    def _collect_cfg_from_widgets(self) -> dict[str, Any]:
        modules = {
            key: bool(self.query_one(f"#mod-{key}", Checkbox).value)
            for key in MODULE_KEYS
        }
        colors = {
            slot: str(self.query_one(f"#col-{slot}", Select).value)
            for slot in COLOR_SLOTS
        }
        capsule_colors = {
            cap_key: str(self.query_one(f"#capcol-{cap_key}", Select).value)
            for cap_key in CAPSULE_COLOR_KEYS
        }
        lang = str(self.query_one("#opt-lang", Select).value)
        style = str(self.query_one("#opt-style", Select).value)
        indicator = str(self.query_one("#opt-indicator", Select).value)
        density = str(self.query_one("#opt-density", Select).value)
        secondary_metric = str(self.query_one("#opt-secondary", Select).value)
        theme = str(self.query_one("#opt-theme", Select).value)
        color_mode = str(self.query_one("#opt-colormode", Select).value)
        quota_precision = str(self.query_one("#opt-quotaprec", Select).value)
        tail_rows = list(self.cfg.get("row_colors") or [])[3:]
        row_colors = [str(self.query_one(f"#rowcol-{idx}", Select).value) for idx in range(3)] + tail_rows
        auto_wrap = bool(self.query_one("#opt-autowrap", Checkbox).value)
        top_gap = bool(self.query_one("#opt-topgap", Checkbox).value)
        line_gap = bool(self.query_one("#opt-linegap", Checkbox).value)
        max_width = int(self.query_one("#opt-maxwidth", Select).value)
        side_ratio = int(self.query_one("#opt-sideratio", Select).value)
        dock_ratio = int(self.query_one("#opt-dockratio", Select).value)
        return {
            "version": 1,
            "language": lang,
            "style": style,
            "indicator": indicator,
            "density": density,
            "secondary_metric": secondary_metric,
            "theme": theme,
            "color_mode": color_mode,
            "quota_precision": quota_precision,
            "row_colors": row_colors,
            "top_gap": top_gap,
            "line_gap": line_gap,
            "auto_wrap": auto_wrap,
            "max_width": max_width,
            "modules": modules,
            "colors": colors,
            "capsule_colors": capsule_colors,
            "control_center": {
                "side_ratio": side_ratio,
                "dock_ratio": dock_ratio,
            },
            "cloud": self.cfg.get("cloud", {}),
        }

    def _update_preview(self) -> None:
        draft = self._collect_cfg_from_widgets()
        t = self._tr(draft["language"])
        preview_cols = int(draft.get("max_width") or 0) or 90
        ansi_preview = self._render_preview(draft, preview_cols=preview_cols)
        rich_out = Text.from_ansi(f"{t['preview_hdr']} ({preview_cols}c)\n{ansi_preview}")
        self.query_one("#preview-box", Static).update(rich_out)

    def _render_preview(self, draft: dict[str, Any], *, preview_cols: int = 90) -> str:
        if not STATUSLINE_SCRIPT.exists():
            return json.dumps(draft, ensure_ascii=False)

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as tf:
            json.dump(draft, tf, ensure_ascii=False)
            tmp_cfg = tf.name
        try:
            env = os.environ.copy()
            env["FAIZ_STATUSLINE_NO_BG"] = "1"
            env["FAIZ_STATUSLINE_CONFIG"] = tmp_cfg
            env["FAIZ_TERM_WIDTH"] = str(preview_cols)
            env["COLORTERM"] = "truecolor"
            res = subprocess.run(
                [str(STATUSLINE_SCRIPT)],
                input=b'{"context_window":{"used_percentage":43}}',
                env=env,
                capture_output=True,
                timeout=2.0,
            )
            return res.stdout.decode("utf-8", errors="replace").strip() or "(empty)"
        except Exception as exc:
            return f"(preview error: {exc})"
        finally:
            try:
                os.unlink(tmp_cfg)
            except OSError:
                pass

    def on_checkbox_changed(self, _: Checkbox.Changed) -> None:
        self._update_preview()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "opt-lang":
            self._apply_i18n(str(event.value))
        self._update_preview()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self.action_save_config()
        elif event.button.id == "btn-reset":
            self.cfg = json.loads(json.dumps(DEFAULT_STATUSLINE_CONFIG))
            for key in MODULE_KEYS:
                self.query_one(f"#mod-{key}", Checkbox).value = True
            self.query_one("#opt-autowrap", Checkbox).value = True
            self.query_one("#opt-topgap", Checkbox).value = bool(DEFAULT_STATUSLINE_CONFIG["top_gap"])
            self.query_one("#opt-linegap", Checkbox).value = bool(DEFAULT_STATUSLINE_CONFIG["line_gap"])
            self.query_one("#opt-autodock", Checkbox).value = False
            self.query_one("#opt-maxwidth", Select).value = int(DEFAULT_STATUSLINE_CONFIG["max_width"])
            self.query_one("#opt-sideratio", Select).value = DEFAULT_SIDE_RATIO
            self.query_one("#opt-dockratio", Select).value = DEFAULT_DOCK_RATIO
            self.query_one("#opt-lang", Select).value = DEFAULT_STATUSLINE_CONFIG["language"]
            self.query_one("#opt-style", Select).value = DEFAULT_STATUSLINE_CONFIG["style"]
            self.query_one("#opt-indicator", Select).value = DEFAULT_STATUSLINE_CONFIG["indicator"]
            self.query_one("#opt-density", Select).value = DEFAULT_STATUSLINE_CONFIG["density"]
            self.query_one("#opt-secondary", Select).value = DEFAULT_STATUSLINE_CONFIG["secondary_metric"]
            self.query_one("#opt-theme", Select).value = DEFAULT_STATUSLINE_CONFIG["theme"]
            self.query_one("#opt-colormode", Select).value = DEFAULT_STATUSLINE_CONFIG["color_mode"]
            self.query_one("#opt-quotaprec", Select).value = DEFAULT_STATUSLINE_CONFIG["quota_precision"]
            for idx in range(3):
                self.query_one(f"#rowcol-{idx}", Select).value = DEFAULT_STATUSLINE_CONFIG["row_colors"][idx]
            for slot in COLOR_SLOTS:
                self.query_one(f"#col-{slot}", Select).value = self.cfg["colors"][slot]
            for cap_key in CAPSULE_COLOR_KEYS:
                self.query_one(f"#capcol-{cap_key}", Select).value = DEFAULT_STATUSLINE_CONFIG["capsule_colors"][cap_key]
            self._apply_i18n(DEFAULT_STATUSLINE_CONFIG["language"])
            self._update_preview()
        elif event.button.id == "btn-quit":
            self.exit()

    def action_save_config(self) -> None:
        draft = self._collect_cfg_from_widgets()
        save_statusline_config(draft, self.config_path)
        self.auto_dock = write_auto_dock_enabled(
            bool(self.query_one("#opt-autodock", Checkbox).value),
            self.disabled_flag_path,
        )
        t = self._tr(draft["language"])
        self.notify(t["saved_msg"].format(path=self.config_path))
        self._update_preview()
