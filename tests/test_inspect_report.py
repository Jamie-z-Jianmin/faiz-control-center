"""Unit and CLI tests for read-only `faiz-dash inspect` diagnostic formatting."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from faiz_dash.__main__ import main as cli_main
from faiz_dash.inspect_report import collect_inspect_payload, format_inspect_output
from faiz_dash.store import (
    WINDOW_5H_SECONDS,
    WINDOW_WEEKLY_SECONDS,
    StateStore,
    compute_quota_pace,
)


def _create_temp_telemetry_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            protocol_version TEXT NOT NULL,
            category TEXT,
            start_time REAL NOT NULL,
            end_time REAL,
            duration_ms REAL,
            total_turns INTEGER DEFAULT 1,
            v_level_passed TEXT,
            status TEXT DEFAULT 'RUNNING'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE tool_invocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            status TEXT NOT NULL,
            duration_ms REAL,
            intercepted_by_t1 INTEGER DEFAULT 0,
            timestamp REAL NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO tasks VALUES ('t1', '1.5', 'code', 100.0, 105.0, 5000.0, 3, 'V2', 'COMPLETED')"
    )
    conn.execute(
        "INSERT INTO tasks VALUES ('t2', '1.5', 'code', 200.0, 202.0, 2000.0, 2, 'V1', 'FAILED')"
    )
    conn.execute(
        "INSERT INTO tool_invocations (task_id, tool_name, status, duration_ms, intercepted_by_t1, timestamp) "
        "VALUES ('t1', 'run_command', 'SUCCESS', 120.0, 0, 101.0)"
    )
    conn.execute(
        "INSERT INTO tool_invocations (task_id, tool_name, status, duration_ms, intercepted_by_t1, timestamp) "
        "VALUES ('t2', 'read_file', 'BLOCKED', 10.0, 1, 201.0)"
    )
    conn.commit()
    conn.close()


class TestInspectReport(unittest.TestCase):
    def test_collect_and_render_full_report_with_redaction(self) -> None:
        now_ep = 1_800_000_000
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            cache.mkdir(parents=True)
            db_path = root / "telemetry.db"
            _create_temp_telemetry_db(db_path)

            (cache / "usage_snapshot.json").write_text(
                json.dumps(
                    {
                        "fetched_at": "2026-11-18T14:59:41+00:00",
                        "account": "dev@example.com",
                        "source": "pty",
                        "pty_fetched_epoch": now_ep - 600,
                        "quota_gate": "THROTTLE_ARGON",
                        "channels": {"pro_count": 2, "gcp_count": 1, "active_auth_method": "consumer"},
                        "buckets": [
                            {
                                "group_name": "GEMINI MODELS",
                                "models": ["Gemini 4 Hydrogen"],
                                "five_hour_pct": 32.3,
                                "five_hour_refresh": "Refreshes in 2h 50m",
                                "weekly_pct": 55.2,
                                "weekly_refresh": "Refreshes in 160h 50m",
                            },
                            {
                                "group_name": "GEMINI 4 ARGON",
                                "models": ["Gemini 4 Argon"],
                                "five_hour_pct": None,
                                "five_hour_refresh": "",
                                "weekly_pct": 13.7,
                                "weekly_refresh": "Refreshes in 160h 7m",
                            },
                        ],
                        "burn": {
                            "tokens_1h": 2_084_374,
                            "tokens_24h": 14_961_700,
                            "cache_hit_rate": 0.9542,
                            "by_model_24h": {
                                "gemini-4-hydrogen-high": 10_038_063,
                                "gemini-4-argon-medium": 4_855_398,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            (cache / "cloud_backoff.json").write_text(
                json.dumps({"next_allowed_epoch": time.time() + 180}),
                encoding="utf-8",
            )
            (cache / "cloud_snapshot.json").write_text(
                json.dumps(
                    {
                        "fetched_at": "2026-11-18T14:59:41+00:00",
                        "colab_state": "DEGRADED",
                        "drive_used_bytes": 200 * 1024**3,
                        "drive_total_bytes": 5 * 1024**4,
                        "drive_free_bytes": 4800 * 1024**3,
                        "rclone_readonly": True,
                        "rclone_shared_client_id": True,
                        "error": "CRITICAL: RATE_LIMIT_EXCEEDED with Bearer secretToken123456",
                        "mailbox": {
                            "fetched_at": "2026-11-18T14:59:46+00:00",
                            "tasks_count": 1,
                            "done_count": 2,
                            "failed_count": 1,
                            "tasks": [{"name": "active_job.json", "queue": "tasks", "size": 120}],
                            "done_items": [
                                {
                                    "name": "stage_b10.tts.json",
                                    "queue": "done",
                                    "modified_at": "2026-11-18 20:49:18",
                                    "verified": True,
                                    "verify_detail": "receipt + 4 wav",
                                },
                                {
                                    "name": "stage_b9.tts.json",
                                    "queue": "done",
                                    "modified_at": "2026-11-18 19:48:21",
                                    "verified": False,
                                    "verify_detail": "no receipt",
                                },
                            ],
                            "failed_items": [
                                {
                                    "name": "stage_b1.tts.json",
                                    "queue": "failed",
                                    "modified_at": "2026-11-18 16:00:00",
                                    "log_tail": "token sk-supersecret99999999 caused failure",
                                }
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )

            store = StateStore(cache_dir=cache)
            payload = collect_inspect_payload(store, telemetry_db=db_path, now_epoch=float(now_ep))

            md = format_inspect_output(payload, as_json=False)
            self.assertIn("THROTTLE_ARGON", md)
            self.assertIn("GEMINI MODELS", md)
            self.assertIn("2h40m", md)  # 2h50m minus 600s elapsed
            self.assertIn("Pace / Forecast", md)
            self.assertIn("5h +21% (~1h06m)", md)
            self.assertIn("gemini-4-hydrogen-high", md)
            self.assertIn("Drive 403 RATE_LIMIT", md)
            self.assertIn("stage_b10.tts.json", md)
            self.assertIn("[VERIFIED]", md)
            self.assertIn("[UNVERIFIED]", md)
            self.assertIn("sk-***", md)
            self.assertNotIn("sk-supersecret99999999", md)
            self.assertIn("Tasks: `2` (`1` completed / `1` failed | `50.0%` success)", md)
            self.assertIn("`run_command` (1)", md)

            raw_json = format_inspect_output(payload, as_json=True)
            parsed = json.loads(raw_json)
            self.assertEqual(parsed["telemetry"]["total_tasks"], 2)
            self.assertEqual(parsed["quota"]["buckets"][0]["five_hour_pace"]["status"], "over")
            self.assertEqual(parsed["quota"]["buckets"][0]["five_hour_pace"]["pace_delta_pct"], 21)
            self.assertIsNotNone(parsed["quota"]["buckets"][0]["five_hour_pace"]["runs_out_seconds"])
            self.assertNotIn("secretToken123456", raw_json)

            # Portable SQLite fallback must work cleanly even when external telemetry.py is absent (CI runners)
            payload_fallback = collect_inspect_payload(
                store,
                telemetry_db=db_path,
                telemetry_script=root / "missing_telemetry.py",
                now_epoch=float(now_ep),
            )
            self.assertEqual(payload_fallback["telemetry"], payload["telemetry"])

    def test_empty_cache_and_cli_inspect_subcommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            (fake_home / ".agents" / "cache").mkdir(parents=True)
            with mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = cli_main(["inspect"])
                self.assertEqual(rc, 0)
                out = buf.getvalue()
                self.assertIn("Faiz Control Center Diagnostic", out)
                self.assertIn("No cached `/usage` snapshot found", out)

                buf_json = io.StringIO()
                with contextlib.redirect_stdout(buf_json):
                    rc_json = cli_main(["inspect", "--json"])
                self.assertEqual(rc_json, 0)
                parsed = json.loads(buf_json.getvalue())
                self.assertIsNone(parsed["quota"])
                self.assertIsNone(parsed["cloud"])

    def test_compute_quota_pace_cases(self) -> None:
        # Over pace with forecast (elapsed >= 600s, est < reset_seconds)
        over = compute_quota_pace(40.0, 9000, WINDOW_5H_SECONDS)
        assert over is not None
        self.assertEqual(over["status"], "over")
        self.assertEqual(over["pace_delta_pct"], 10)
        self.assertEqual(over["runs_out_seconds"], 6000)

        # Warm-up window (<600s elapsed) suppresses run-out forecast
        warmup = compute_quota_pace(85.0, WINDOW_5H_SECONDS - 300, WINDOW_5H_SECONDS)
        assert warmup is not None
        self.assertEqual(warmup["status"], "over")
        self.assertIsNone(warmup["runs_out_seconds"])

        # Under pace has headroom and no run-out forecast
        under = compute_quota_pace(85.0, 3600, WINDOW_5H_SECONDS)
        assert under is not None
        self.assertEqual(under["status"], "under")
        self.assertLess(under["pace_delta_pct"], 0)
        self.assertIsNone(under["runs_out_seconds"])

        # On pace within [-4, 4]
        on = compute_quota_pace(48.0, 9000, WINDOW_5H_SECONDS)
        assert on is not None
        self.assertEqual(on["status"], "on_pace")

        # Invalid/out-of-range inputs return None
        self.assertIsNone(compute_quota_pace(None, 3600, WINDOW_5H_SECONDS))
        self.assertIsNone(compute_quota_pace(50.0, None, WINDOW_5H_SECONDS))
        self.assertIsNone(compute_quota_pace(50.0, -1, WINDOW_WEEKLY_SECONDS))
        self.assertIsNone(compute_quota_pace(50.0, WINDOW_5H_SECONDS + 10, WINDOW_5H_SECONDS))


if __name__ == "__main__":
    unittest.main()
