"""Unit tests for Colab timestamp parsing, STALE_OFFLINE, backoff, and rclone.conf."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faiz_dash.collectors.cloud_probe import CloudProbe, parse_colab_timestamp
from faiz_dash.gateways.drive import RcloneDriveGateway, inspect_rclone_conf


class FakeDriveGateway:
    def __init__(self, status_payload: object = None, should_fail: bool = False) -> None:
        self.status_payload = status_payload
        self.should_fail = should_fail

    def capabilities(self) -> dict[str, object]:
        return {"read": True, "write": False, "readonly": True, "shared_client_id": True, "warnings": []}

    async def cat_text(self, remote_path: str) -> str:
        if self.should_fail:
            raise TimeoutError("rclone timed out after 12.0s")
        return json.dumps(self.status_payload)

    async def lsf(self, remote_path: str, dirs_only: bool = False) -> list[str]:
        return []

    async def lsl(self, remote_path: str, max_depth: int = 1) -> list[dict[str, object]]:
        return []

    async def about(self) -> dict[str, object]:
        return {"used": 1000, "total": 5000, "free": 4000}


class TestCloudProbe(unittest.IsolatedAsyncioTestCase):
    def test_parse_colab_timestamp_formats(self) -> None:
        iso_dt = parse_colab_timestamp("2026-11-18T10:24:29Z")
        self.assertEqual(iso_dt, datetime(2026, 11, 18, 10, 24, 29, tzinfo=timezone.utc))

        gnu_utc = parse_colab_timestamp("Wed Nov 18 09:06:22 AM UTC 2026")
        self.assertEqual(gnu_utc, datetime(2026, 11, 18, 9, 6, 22, tzinfo=timezone.utc))

        gnu_pm = parse_colab_timestamp("Wed Nov 18 01:06:22 PM UTC 2026")
        self.assertEqual(gnu_pm, datetime(2026, 11, 18, 13, 6, 22, tzinfo=timezone.utc))

        gnu_cst = parse_colab_timestamp("Wed Nov 18 09:06:22 AM CST 2026")
        self.assertEqual(gnu_cst, datetime(2026, 11, 18, 1, 6, 22, tzinfo=timezone.utc))

        gnu_pdt = parse_colab_timestamp("Wed Nov 18 09:06:22 AM PDT 2026")
        self.assertEqual(gnu_pdt, datetime(2026, 11, 18, 16, 6, 22, tzinfo=timezone.utc))

        gnu_pst = parse_colab_timestamp("Wed Nov 18 09:06:22 AM PST 2026")
        self.assertEqual(gnu_pst, datetime(2026, 11, 18, 17, 6, 22, tzinfo=timezone.utc))

    async def test_colab_online_stale_stopped_and_degraded_backoff(self) -> None:
        drive = FakeDriveGateway(
            status_payload={
                "status": "ONLINE",
                "project": "demo_proj",
                "mode": "tts_only",
                "idle_seconds": 1420,
                "updated_at": "Wed Nov 18 10:24:29 AM UTC 2026",
                "api_url": "https://example.trycloudflare.com",
            }
        )
        probe = CloudProbe(drive, gcloud_bin="/usr/bin/true")

        # 90s later -> ONLINE
        now_ok = datetime(2026, 11, 18, 10, 25, 59, tzinfo=timezone.utc)
        snap_ok = await probe.collect(now_utc=now_ok)
        self.assertEqual(snap_ok.colab_state, "ONLINE")
        self.assertEqual(snap_ok.age_seconds, 90)
        self.assertEqual(snap_ok.idle_remaining_seconds, 3600 - 1420)

        # 200s later -> STALE_OFFLINE (> 180s)
        now_stale = datetime(2026, 11, 18, 10, 27, 49, tzinfo=timezone.utc)
        snap_stale = await probe.collect(now_utc=now_stale)
        self.assertEqual(snap_stale.colab_state, "STALE_OFFLINE")
        self.assertEqual(snap_stale.age_seconds, 200)

        # Failure -> DEGRADED preserving last project & exponential backoff 60 -> 120 -> 300
        drive.should_fail = True
        snap_deg1 = await probe.collect(force=True, now_utc=now_stale)
        self.assertEqual(snap_deg1.colab_state, "DEGRADED")
        self.assertTrue(snap_deg1.stale)
        self.assertEqual(snap_deg1.project, "demo_proj")
        self.assertEqual(probe.next_backoff_seconds(), 60.0)

        await probe.collect(force=True, now_utc=now_stale)
        self.assertEqual(probe.next_backoff_seconds(), 120.0)

        await probe.collect(force=True, now_utc=now_stale)
        self.assertEqual(probe.next_backoff_seconds(), 300.0)

        # STOPPED state
        drive.should_fail = False
        drive.status_payload = {
            "status": "STOPPED_IDLE_TIMEOUT",
            "project": "demo_proj",
            "mode": "tts_only",
            "reason": "GPU idle > 60min",
            "stopped_at": "Wed Nov 18 09:06:22 AM UTC 2026",
        }
        snap_stop = await probe.collect(force=True, now_utc=now_stale)
        self.assertEqual(snap_stop.colab_state, "STOPPED")
        self.assertEqual(snap_stop.idle_remaining_seconds, 0)

    def test_inspect_rclone_conf_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "rclone.conf"
            conf.write_text(
                "[gdrive]\ntype = drive\nscope = drive.readonly\nclient_id =\nclient_secret =\n",
                encoding="utf-8",
            )
            info = inspect_rclone_conf(conf)
            self.assertTrue(info["readonly"])
            self.assertTrue(info["shared_client_id"])
            self.assertEqual(len(info["warnings"]), 2)
            self.assertEqual(RcloneDriveGateway(conf_path=conf).timeout, 30.0)

    async def test_persistent_rate_limit_backoff_across_instances(self) -> None:
        class CountingRateLimitDrive(FakeDriveGateway):
            def __init__(self) -> None:
                super().__init__(should_fail=True)
                self.cat_calls = 0

            async def cat_text(self, remote_path: str) -> str:
                self.cat_calls += 1
                raise RuntimeError("403 RATE_LIMIT_EXCEEDED: userRateLimitExceeded")

        with tempfile.TemporaryDirectory() as tmp:
            backoff_file = Path(tmp) / "cloud_backoff.json"
            drive1 = CountingRateLimitDrive()
            p1 = CloudProbe(drive1, gcloud_bin="/usr/bin/true", backoff_path=backoff_file)
            t0 = datetime(2026, 11, 18, 14, 0, 0, tzinfo=timezone.utc)
            snap1 = await p1.collect(force=False, now_utc=t0)
            self.assertEqual(drive1.cat_calls, 1)
            self.assertEqual(snap1.colab_state, "DEGRADED")
            self.assertTrue(p1.in_backoff_skip)
            self.assertTrue(backoff_file.exists())

            # Second short-lived CloudProbe instance 60s later must obey persisted 180s cooldown
            drive2 = CountingRateLimitDrive()
            p2 = CloudProbe(drive2, gcloud_bin="/usr/bin/true", backoff_path=backoff_file)
            t1 = datetime(2026, 11, 18, 14, 1, 0, tzinfo=timezone.utc)
            snap2 = await p2.collect(force=False, now_utc=t1)
            self.assertEqual(drive2.cat_calls, 0)
            self.assertTrue(p2.in_backoff_skip)
            self.assertTrue(snap2.stale)
            self.assertIn("RATE_LIMIT", snap2.error or "")

    async def test_unconfigured_rclone_returns_not_configured_without_error(self) -> None:
        class UnconfiguredDrive(FakeDriveGateway):
            def capabilities(self) -> dict[str, object]:
                return {
                    "configured": False,
                    "read": True,
                    "write": False,
                    "readonly": False,
                    "shared_client_id": False,
                    "warnings": [],
                }

        probe = CloudProbe(UnconfiguredDrive(), gcloud_bin="/nonexistent/gcloud")
        snap = await probe.collect()
        self.assertEqual(snap.colab_state, "NOT_CONFIGURED")
        self.assertIsNone(snap.error)
        self.assertFalse(snap.stale)


if __name__ == "__main__":
    unittest.main()
