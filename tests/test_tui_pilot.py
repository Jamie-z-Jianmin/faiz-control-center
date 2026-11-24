"""Headless Textual pilot integration tests for 140x12 Dock and 46x36 Sidebar."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faiz_dash.models import (
    ColabGcpSnapshot,
    GceInstanceInfo,
    MailboxItem,
    MailboxSnapshot,
    ModelBucketQuota,
    ProcessChannelInfo,
    QuotaSnapshot,
    TokenBurnStats,
)
from faiz_dash.statusline_config import load_statusline_config
from faiz_dash.store import StateStore
from faiz_dash.ui.app import FaizControlCenterApp
from faiz_dash.ui.config_app import StatuslineConfigApp
from faiz_dash.ui.layout import LayoutManager


def _build_demo_store(cache_dir: Path) -> StateStore:
    store = StateStore(cache_dir=cache_dir)
    store.quota = QuotaSnapshot(
        fetched_at="2026-11-18T10:00:00Z",
        account="user@example.com",
        buckets=(
            ModelBucketQuota("GEMINI MODELS", ("Gemini 4 Hydrogen",), 90.73, "Refreshes in 165h 24m", 69.09, "Refreshes in 2h 24m"),
            ModelBucketQuota("CLAUDE AND GPT MODELS", ("Claude Opus 5.5",), 100.0, "Quota available", 100.0, "Quota available"),
            ModelBucketQuota("GEMINI 4 ARGON", ("Gemini 4 Argon",), 13.78, "Refreshes in 164h 41m"),
        ),
        channels=ProcessChannelInfo(pro_count=2, gcp_count=0),
        burn=TokenBurnStats(tokens_1h=12500, tokens_24h=85000, cache_hit_rate=0.78, active_sessions=2),
        quota_gate="THROTTLE_ARGON",
        source="pty",
    )
    store.cloud = ColabGcpSnapshot(
        fetched_at="2026-11-18T10:00:00Z",
        colab_state="ONLINE",
        raw_status="ONLINE",
        project="demo_swarm",
        mode="tts_only",
        age_seconds=45,
        idle_remaining_seconds=2180,
        gce_instances=(GceInstanceInfo("gpu-spot-1", "us-central1-a", "TERMINATED"),),
        drive_used_bytes=178 * 1024**3,
        drive_total_bytes=5 * 1024**4,
        rclone_readonly=True,
        rclone_shared_client_id=True,
    )
    store.mailbox = MailboxSnapshot(
        fetched_at="2026-11-18T10:00:00Z",
        tasks_count=0,
        done_count=10,
        failed_count=1,
        done_items=(
            MailboxItem("stage_b5_native.tts.json", "done", is_tts=True, verified=True, verify_detail="03_Audio: receipt + 6 wav"),
        ),
        failed_items=(
            MailboxItem("stage_b1_ab_golden.tts.json", "failed", is_tts=True, verified=False, log_tail="Exit code 1"),
        ),
    )
    return store


class TestTuiPilot(unittest.IsolatedAsyncioTestCase):
    async def test_dock_140x12_interactions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _build_demo_store(root / "cache")
            lm = LayoutManager(layout_path=root / "tui_layout.json")
            app = FaizControlCenterApp(
                dock=True,
                preset=1,
                store=store,
                layout_manager=lm,
                enable_background_timers=False,
            )
            async with app.run_test(size=(140, 12)) as pilot:
                await pilot.pause()
                container = app.query_one("#panels-container")
                self.assertTrue(container.has_class("preset-horizontal"))

                # Toggle preset 2 -> vertical, then back to 1 -> horizontal
                await pilot.press("2")
                self.assertTrue(container.has_class("preset-vertical"))
                await pilot.press("1")
                self.assertTrue(container.has_class("preset-horizontal"))

                # Adjust fr with [ and ]
                await pilot.press("]")
                self.assertAlmostEqual(lm.panels["quota"].fr, 1.1)
                await pilot.press("[")
                self.assertAlmostEqual(lm.panels["quota"].fr, 1.0)

                # Zoom m and unzoom m
                await pilot.press("m")
                self.assertEqual(lm.zoomed_panel, "quota")
                self.assertFalse(app.query_one("#panel-colab").display)
                await pilot.press("m")
                self.assertIsNone(lm.zoomed_panel)
                self.assertTrue(app.query_one("#panel-colab").display)

                # Swap s
                await pilot.press("s")
                ordered_ids = [c.panel_id for c in lm.ordered_panels()]
                self.assertEqual(ordered_ids, ["colab", "quota", "mailbox"])

                # Toggle visibility v (hides focused panel 'quota'), then 0 shows all
                await pilot.press("v")
                self.assertFalse(lm.panels["quota"].visible)
                await pilot.press("0")
                self.assertTrue(lm.panels["quota"].visible)

    async def test_narrow_sidebar_46x36_auto_preset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _build_demo_store(root / "cache")
            lm = LayoutManager(layout_path=root / "tui_layout.json")
            app = FaizControlCenterApp(
                dock=False,
                preset=1,
                store=store,
                layout_manager=lm,
                enable_background_timers=False,
            )
            async with app.run_test(size=(46, 36)) as pilot:
                await pilot.pause()
                container = app.query_one("#panels-container")
                # Auto-switches to vertical at width <= 55
                self.assertTrue(container.has_class("preset-vertical"))
                await pilot.press("tab")
                await pilot.press("m")
                self.assertIsNotNone(lm.zoomed_panel)
                await pilot.press("m")
                self.assertIsNone(lm.zoomed_panel)

    async def test_statusline_config_app_saves_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "statusline_config.json"
            disabled_flag = Path(tmp) / "dash_disabled"
            disabled_flag.write_text("", encoding="utf-8")
            app = StatuslineConfigApp(config_path=cfg_path, disabled_flag_path=disabled_flag)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                self.assertIn("Modules", str(app.query_one("#sec-modules").render()))
                self.assertFalse(bool(app.query_one("#opt-autodock").value))  # type: ignore[attr-defined]
                app.query_one("#mod-sessions").value = False  # type: ignore[attr-defined]
                app.query_one("#mod-speed").value = False  # type: ignore[attr-defined]
                app.query_one("#opt-topgap").value = False  # type: ignore[attr-defined]
                app.query_one("#opt-linegap").value = False  # type: ignore[attr-defined]
                app.query_one("#opt-autodock").value = True  # type: ignore[attr-defined]
                app.query_one("#opt-maxwidth").value = 90  # type: ignore[attr-defined]
                app.query_one("#opt-sideratio").value = 32  # type: ignore[attr-defined]
                app.query_one("#opt-dockratio").value = 26  # type: ignore[attr-defined]
                app.query_one("#opt-lang").value = "zh"  # type: ignore[attr-defined]
                app.query_one("#opt-style").value = "powerline"  # type: ignore[attr-defined]
                app.query_one("#opt-indicator").value = "bar"  # type: ignore[attr-defined]
                app.query_one("#opt-density").value = "expanded"  # type: ignore[attr-defined]
                app.query_one("#opt-secondary").value = "reset"  # type: ignore[attr-defined]
                app.query_one("#opt-theme").value = "tokyo_night"  # type: ignore[attr-defined]
                app.query_one("#opt-colormode").value = "semantic"  # type: ignore[attr-defined]
                app.query_one("#opt-quotaprec").value = "1dp"  # type: ignore[attr-defined]
                app.query_one("#rowcol-0").value = "red"  # type: ignore[attr-defined]
                app.query_one("#capcol-gemini").value = "green"  # type: ignore[attr-defined]
                await pilot.pause()
                self.assertIn("启用模块", str(app.query_one("#sec-modules").render()))
                self.assertIn("控制中心", str(app.query_one("#sec-control").render()))
                await pilot.press("s")
                await pilot.pause()
                saved = load_statusline_config(cfg_path)
                self.assertFalse(disabled_flag.exists())
                self.assertEqual(saved["max_width"], 90)
                self.assertEqual(saved["control_center"], {"side_ratio": 32, "dock_ratio": 26})
                self.assertEqual(saved["language"], "zh")
                self.assertEqual(saved["style"], "powerline")
                self.assertEqual(saved["indicator"], "bar")
                self.assertEqual(saved["density"], "expanded")
                self.assertEqual(saved["secondary_metric"], "reset")
                self.assertEqual(saved["theme"], "tokyo_night")
                self.assertEqual(saved["color_mode"], "semantic")
                self.assertEqual(saved["quota_precision"], "1dp")
                self.assertEqual(saved["row_colors"][0], "red")
                self.assertEqual(saved["capsule_colors"]["gemini"], "green")
                self.assertEqual(saved["capsule_colors"]["context"], "auto")
                self.assertFalse(saved["top_gap"])
                self.assertFalse(saved["line_gap"])
                self.assertFalse(saved["modules"]["speed"])
                self.assertFalse(saved["modules"]["sessions"])

    async def test_tui_cold_start_hydration_multi_failure_and_backoff_display(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            cache_dir.mkdir(parents=True)
            (cache_dir / "usage_snapshot.json").write_text(
                json.dumps(
                    {
                        "fetched_at": "2026-11-18T14:17:28Z",
                        "buckets": [
                            {"group_name": "GEMINI MODELS", "five_hour_pct": 60.5, "weekly_pct": 62.8, "five_hour_refresh": "Refreshes in 3h 22m"},
                            {"group_name": "GEMINI 4 ARGON", "weekly_pct": 13.8},
                        ],
                        "burn": {"tokens_1h": 2076085, "tokens_24h": 13149601, "cache_hit_rate": 0.95},
                        "quota_gate": "THROTTLE_ARGON",
                    }
                ),
                encoding="utf-8",
            )
            (cache_dir / "cloud_snapshot.json").write_text(
                json.dumps(
                    {
                        "fetched_at": "2026-11-18T14:17:29Z",
                        "colab_state": "DEGRADED",
                        "raw_status": "ONLINE",
                        "project": "DemoSwarm",
                        "error": "Colab: 2026/11/18 NOTICE: gdrive shared client_id ... 403 RATE_LIMIT_EXCEEDED",
                        "stale": True,
                        "mailbox": {
                            "fetched_at": "2026-11-18T14:17:29Z",
                            "tasks_count": 1,
                            "done_count": 17,
                            "failed_count": 2,
                            "tasks": [{"name": "stage_b11_next.tts.json", "queue": "tasks"}],
                            "failed_items": [
                                {"name": "stage_b10_voxcpm2.tts.json", "queue": "failed", "log_tail": "CUDA OOM"},
                                {"name": "stage_b9_failed.tts.json", "queue": "failed", "log_tail": "Exit 1"},
                            ],
                            "done_items": [],
                            "error": "403 RATE_LIMIT_EXCEEDED",
                            "stale": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (cache_dir / "cloud_backoff.json").write_text(
                json.dumps({"next_allowed_epoch": time.time() + 150, "error_streak": 1}),
                encoding="utf-8",
            )

            store = StateStore(cache_dir=cache_dir)
            lm = LayoutManager(layout_path=root / "tui_layout.json")
            app = FaizControlCenterApp(
                dock=True,
                preset=1,
                store=store,
                layout_manager=lm,
                enable_background_timers=False,
            )
            async with app.run_test(size=(140, 12)) as pilot:
                await pilot.pause()
                q_txt = str(app.query_one("#panel-quota").render())
                c_txt = str(app.query_one("#panel-colab").render())
                m_txt = str(app.query_one("#panel-mailbox").render())
                self.assertIn("60.5%", q_txt)
                self.assertNotIn("Waiting", q_txt)
                self.assertIn("Drive 403 RATE_LIMIT", c_txt)
                self.assertIn("cooldown", c_txt)
                self.assertIn("stage_b10_voxcpm2.tts.json", m_txt)
                self.assertIn("stage_b9_failed.tts.json", m_txt)
                self.assertIn("stage_b11_next.tts.json", m_txt)

    async def test_quota_panel_two_bucket_vs_legacy_three_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _build_demo_store(root / "cache")
            lm = LayoutManager(layout_path=root / "tui_layout.json")
            app = FaizControlCenterApp(
                dock=True,
                preset=1,
                store=store,
                layout_manager=lm,
                enable_background_timers=False,
            )
            async with app.run_test(size=(140, 12)) as pilot:
                await pilot.pause()
                panel = app.query_one("#panel-quota")
                legacy_txt = str(panel.render())
                self.assertIn("Argon", legacy_txt)
                self.assertIn("C/G", legacy_txt)

                two_bucket = QuotaSnapshot(
                    fetched_at="2026-11-24T06:00:00Z",
                    account="user@example.com",
                    buckets=(
                        ModelBucketQuota("GEMINI MODELS", ("Gemini 4 Hydrogen",), 90.73, "Refreshes in 165h 24m", 69.09, "Refreshes in 2h 24m"),
                        ModelBucketQuota("CLAUDE AND GPT MODELS", ("Claude Opus 5.5",), 88.25, "Refreshes in 120h 0m", 70.5, "Refreshes in 3h 10m"),
                    ),
                    channels=ProcessChannelInfo(pro_count=1, gcp_count=0),
                    burn=TokenBurnStats(tokens_1h=1000, tokens_24h=5000, cache_hit_rate=0.8, active_sessions=1),
                    quota_gate="GREEN",
                    source="pty",
                )
                panel.update_snapshot(two_bucket)  # type: ignore[attr-defined]
                await pilot.pause()
                new_txt = str(panel.render())
                self.assertIn("Claude", new_txt)
                self.assertIn("88.2%", new_txt)
                self.assertIn("5h 70.5%", new_txt)
                self.assertNotIn("Argon", new_txt)
                self.assertNotIn("C/G", new_txt)


if __name__ == "__main__":
    unittest.main()
