"""CLI entrypoint for `python -m faiz_dash`."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Optional

from .collectors.cloud_probe import CloudProbe
from .collectors.mailbox_probe import MailboxProbe
from .collectors.usage_probe import UsageProbe
from .gateways.drive import RcloneDriveGateway
from .inspect_report import collect_inspect_payload, format_inspect_output
from .statusline_config import (
    COLOR_SLOTS,
    MODULE_KEYS,
    VALID_COLORS,
    VALID_COLOR_MODES,
    VALID_DENSITIES,
    VALID_INDICATORS,
    VALID_LANGUAGES,
    VALID_QUOTA_PRECISIONS,
    VALID_ROW_COLOR_TOKENS,
    VALID_SECONDARY_METRICS,
    VALID_STYLES,
    VALID_THEMES,
    load_statusline_config,
    read_auto_dock_enabled,
    save_statusline_config,
    write_auto_dock_enabled,
)
from .store import StateStore
from .ui.app import FaizControlCenterApp
from .ui.config_app import StatuslineConfigApp


async def _collect_live_store(
    skip_pty: bool = False,
    quota_only: bool = False,
    cache_dir: Optional[Path] = None,
) -> StateStore:
    sem = asyncio.Semaphore(3)
    store = StateStore(cache_dir=cache_dir or (Path.home() / ".agents" / "cache"))
    drive = RcloneDriveGateway(semaphore=sem)
    usage = UsageProbe(cache_dir=store.cache_dir, semaphore=sem)

    async def _collect_and_publish_quota() -> None:
        quota_snap = await usage.collect(probe_pty=not skip_pty, force_pty=False)
        await store.publish_quota(quota_snap)

    if quota_only:
        await _collect_and_publish_quota()
        return store

    cloud = CloudProbe(drive, semaphore=sem, backoff_path=store.cache_dir / "cloud_backoff.json")
    mailbox = MailboxProbe(drive)

    async def _collect_cloud_and_mailbox() -> None:
        cloud_snap = await cloud.collect(force=False)
        skip_mbox = (
            cloud_snap.colab_state == "NOT_CONFIGURED"
            or cloud.in_backoff_skip
            or ("RATE_LIMIT" in (cloud_snap.error or "").upper())
        )
        mbox_snap = await mailbox.collect(
            default_project=cloud_snap.project,
            colab_online=(cloud_snap.colab_state == "ONLINE"),
            webui_url=cloud_snap.webui_url,
            skip_drive=skip_mbox,
            skip_error=cloud_snap.error,
        )
        await store.publish_cloud_and_mailbox(cloud_snap, mbox_snap)

    await asyncio.gather(_collect_and_publish_quota(), _collect_cloud_and_mailbox())
    return store


async def _run_once_json(skip_pty: bool = False, quota_only: bool = False) -> dict[str, object]:
    store = await _collect_live_store(skip_pty=skip_pty, quota_only=quota_only)
    return store.to_combined_dict()


def _run_config_cmd(args: argparse.Namespace) -> int:
    has_cli_flags = any(
        [
            args.show,
            args.lang is not None,
            args.style is not None,
            args.indicator is not None,
            args.density is not None,
            args.secondary is not None,
            args.theme is not None,
            args.color_mode is not None,
            args.quota_precision is not None,
            args.row_colors is not None,
            args.top_gap is not None,
            args.line_gap is not None,
            args.enable is not None,
            args.disable is not None,
            args.color is not None,
            args.auto_wrap is not None,
            args.max_width is not None,
            args.side_ratio is not None,
            args.dock_ratio is not None,
            args.auto_dock is not None,
            args.reset,
        ]
    )
    if not has_cli_flags:
        StatuslineConfigApp().run()
        return 0

    if args.reset:
        from .statusline_config import DEFAULT_STATUSLINE_CONFIG

        cfg = save_statusline_config(DEFAULT_STATUSLINE_CONFIG)
    else:
        cfg = load_statusline_config()

    if args.lang is not None:
        cfg["language"] = args.lang
    if args.style is not None:
        cfg["style"] = args.style
    if args.indicator is not None:
        cfg["indicator"] = args.indicator
    if args.density is not None:
        cfg["density"] = args.density
    if args.secondary is not None:
        cfg["secondary_metric"] = args.secondary
    if args.theme is not None:
        cfg["theme"] = args.theme
    if args.color_mode is not None:
        cfg["color_mode"] = args.color_mode
    if args.quota_precision is not None:
        cfg["quota_precision"] = args.quota_precision
    if args.row_colors is not None:
        tokens = [t.strip().lower() for t in args.row_colors.split(",") if t.strip()]
        invalid = [t for t in tokens if t not in VALID_ROW_COLOR_TOKENS]
        if invalid:
            raise SystemExit(f"Unknown row color '{invalid[0]}' (valid: {', '.join(VALID_ROW_COLOR_TOKENS)})")
        if tokens:
            cfg["row_colors"] = tokens
    if args.top_gap is not None:
        cfg["top_gap"] = args.top_gap == "on"
    if args.line_gap is not None:
        cfg["line_gap"] = args.line_gap == "on"
    if args.auto_wrap is not None:
        cfg["auto_wrap"] = args.auto_wrap == "on"
    if args.max_width is not None:
        cfg["max_width"] = max(0, int(args.max_width))
    if args.side_ratio is not None:
        cfg.setdefault("control_center", {})["side_ratio"] = int(args.side_ratio)
    if args.dock_ratio is not None:
        cfg.setdefault("control_center", {})["dock_ratio"] = int(args.dock_ratio)
    if args.auto_dock is not None:
        write_auto_dock_enabled(args.auto_dock == "on")
    if args.enable:
        for item in args.enable.split(","):
            key = item.strip()
            if key in MODULE_KEYS:
                cfg["modules"][key] = True
            elif key:
                raise SystemExit(f"Unknown module: {key} (valid: {', '.join(MODULE_KEYS)})")
    if args.disable:
        for item in args.disable.split(","):
            key = item.strip()
            if key in MODULE_KEYS:
                cfg["modules"][key] = False
            elif key:
                raise SystemExit(f"Unknown module: {key} (valid: {', '.join(MODULE_KEYS)})")
    if args.color:
        for pair in args.color:
            if "=" not in pair:
                raise SystemExit(f"Invalid --color '{pair}', expected slot=color")
            slot, val = (p.strip().lower() for p in pair.split("=", 1))
            if slot not in COLOR_SLOTS:
                raise SystemExit(f"Unknown color slot '{slot}' (valid: {', '.join(COLOR_SLOTS)})")
            if val not in VALID_COLORS:
                raise SystemExit(f"Unknown color '{val}' (valid: {', '.join(VALID_COLORS)})")
            cfg["colors"][slot] = val

    saved = save_statusline_config(cfg)
    saved["auto_dock"] = read_auto_dock_enabled()
    print(json.dumps(saved, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="faiz-dash", description="Faiz Control Center v0.1")
    parser.add_argument("--dock", action="store_true", help="Launch in compact Rex Dock mode")
    parser.add_argument(
        "--preset",
        type=int,
        choices=(1, 2),
        default=None,
        help="Layout preset: 1 (horizontal 3-col dock) or 2 (vertical sidebar)",
    )
    parser.add_argument(
        "--once-json",
        action="store_true",
        help="Collect snapshots once, write local caches, and print JSON",
    )
    parser.add_argument(
        "--no-pty",
        action="store_true",
        help="With --once-json, skip PTY /usage probe and use local/cache only",
    )
    parser.add_argument(
        "--quota-only",
        action="store_true",
        help="With --once-json, refresh quota/usage only without probing Drive/Cloud",
    )
    sub = parser.add_subparsers(dest="subcmd")
    cfg_p = sub.add_parser("config", help="Configure statusLine modules, colors, language, and wrapping")
    cfg_p.add_argument("--show", action="store_true", help="Print current statusline_config.json")
    cfg_p.add_argument("--reset", action="store_true", help="Reset statusline_config.json to defaults")
    cfg_p.add_argument("--lang", choices=VALID_LANGUAGES, help="Set capsule language (en|zh)")
    cfg_p.add_argument("--style", choices=VALID_STYLES, help="Set capsule visual style (ghost|capsule|powerline|pill|minimal)")
    cfg_p.add_argument("--indicator", choices=VALID_INDICATORS, help="Set quota/context indicator (ring|bar)")
    cfg_p.add_argument("--density", choices=VALID_DENSITIES, help="Capsule density (auto=aligned grid <=155c | compact | expanded)")
    cfg_p.add_argument("--secondary", choices=VALID_SECONDARY_METRICS, help="Matrix Col 2 metric (burn | reset | sessions)")
    cfg_p.add_argument("--theme", choices=VALID_THEMES, help="Set TrueColor palette theme (google|nord|tokyo_night|catppuccin|custom)")
    cfg_p.add_argument("--color-mode", choices=VALID_COLOR_MODES, help="Color strategy (row=uniform per row | semantic=per status)")
    cfg_p.add_argument("--quota-precision", choices=VALID_QUOTA_PRECISIONS, help="Quota display precision (exact=2dp | 1dp | int)")
    cfg_p.add_argument("--row-colors", help="Comma-separated row colors for row mode (blue,green,yellow,red,slate,muted)")
    cfg_p.add_argument("--top-gap", choices=("on", "off"), help="Insert blank line below agy model header")
    cfg_p.add_argument("--line-gap", choices=("on", "off"), help="Insert blank breathing line between wrapped rows")
    cfg_p.add_argument("--auto-wrap", choices=("on", "off"), help="Auto-wrap across lines by terminal width")
    cfg_p.add_argument("--max-width", type=int, help="Override max columns (0 = auto sniff)")
    cfg_p.add_argument("--side-ratio", type=int, help="Rex right sidebar width percent (18..45)")
    cfg_p.add_argument("--dock-ratio", type=int, help="Rex bottom dock height percent (12..40)")
    cfg_p.add_argument("--auto-dock", choices=("on", "off"), help="Auto-open bottom Dock on startup")
    cfg_p.add_argument("--enable", help="Comma-separated modules to enable")
    cfg_p.add_argument("--disable", help="Comma-separated modules to disable")
    cfg_p.add_argument("--color", action="append", help="Set slot=color (repeatable)")

    insp_p = sub.add_parser("inspect", help="Print a non-interactive diagnostic report for in-chat inspection")
    insp_p.add_argument("--live", action="store_true", help="Run live quota/cloud/mailbox probes before formatting")
    insp_p.add_argument("--json", action="store_true", help="Print raw sanitized JSON payload instead of Markdown")
    insp_p.add_argument("--no-pty", action="store_true", help="With --live, skip PTY /usage probe")
    return parser


def _run_inspect_cmd(args: argparse.Namespace) -> int:
    cache_dir = Path.home() / ".agents" / "cache"
    store = (
        asyncio.run(_collect_live_store(skip_pty=args.no_pty, cache_dir=cache_dir))
        if args.live
        else StateStore(cache_dir=cache_dir)
    )
    payload = collect_inspect_payload(store)
    print(format_inspect_output(payload, as_json=args.json))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.subcmd == "config":
        return _run_config_cmd(args)
    if args.subcmd == "inspect":
        return _run_inspect_cmd(args)

    if args.once_json:
        payload = asyncio.run(_run_once_json(skip_pty=args.no_pty, quota_only=args.quota_only))
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    app = FaizControlCenterApp(dock=args.dock, preset=args.preset)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
