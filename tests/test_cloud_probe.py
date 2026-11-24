"""Unit tests for Colab timestamp parsing, STALE_OFFLINE, backoff, and rclone.conf."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faiz_dash.collectors.cloud_probe import CloudProbe, parse_colab_timestamp
from faiz_dash.gateways.colab import ColabCliGateway, parse_colab_sessions, parse_colab_usage
from faiz_dash.gateways.drive import (
    GdriveCliDriveGateway,
    RcloneDriveGateway,
    create_drive_gateway,
    inspect_rclone_conf,
    parse_gdrive_quota,
)
from faiz_dash.gateways.exec_util import CommandResult
from faiz_dash.models import GceInstanceInfo


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

    def test_parse_colab_sessions_and_usage(self) -> None:
        empty_out = "[colab] No active sessions found on server.\n"
        self.assertEqual(parse_colab_sessions(empty_out), ())

        active_out = (
            "[faiz-tts] https://m-s-abc123.colab.googleusercontent.com/ "
            "| Hardware: L4 | Shape: STANDARD | Variant: GPU | Status: IDLE\n"
            "[aux-cpu] https://m-s-xyz.colab.googleusercontent.com/ "
            "| Hardware: CPU | Shape: HIGHMEM | Variant: DEFAULT\n"
        )
        parsed = parse_colab_sessions(active_out)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["name"], "faiz-tts")
        self.assertEqual(parsed[0]["hardware"], "L4")
        self.assertEqual(parsed[0]["shape"], "STANDARD")
        self.assertEqual(parsed[0]["variant"], "GPU")
        self.assertEqual(parsed[0]["status"], "IDLE")
        self.assertEqual(parsed[1]["hardware"], "CPU")

        usage_out = (
            "Current balance: 484.01 compute units\n"
            "Usage rate: 0.00/hr\n"
            "Active assignments: 0\n"
        )
        usage = parse_colab_usage(usage_out)
        self.assertAlmostEqual(usage["balance_cu"], 484.01)
        self.assertEqual(usage["usage_rate"], "0.00/hr")
        self.assertEqual(usage["active_assignments"], 0)

    async def test_gdrive_cli_gateway_cache_lsl_quota_and_factory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            id_cache = root / "folder_ids.json"
            calls: list[tuple[str, ...]] = []

            async def fake_run_exec(argv, **kwargs):  # type: ignore[no-untyped-def]
                calls.append(tuple(argv))
                args = list(argv)
                if "quota" in args:
                    return CommandResult(
                        returncode=0,
                        stdout="Used:  143.7 GB\nLimit: 5120.0 GB\nTrash: 0 B\n",
                        stderr="",
                    )
                if "ls" in args:
                    # `readonly ls [folderID] --json`
                    idx = args.index("ls")
                    next_arg = args[idx + 1] if idx + 1 < len(args) else "--json"
                    folder_id = "root" if next_arg.startswith("--") else next_arg
                    if folder_id == "root":
                        payload = [
                            {
                                "id": "fld-colab",
                                "name": "Colab_Notebooks",
                                "mimeType": "application/vnd.google-apps.folder",
                                "modifiedTime": "2026-11-18T10:00:00Z",
                            }
                        ]
                    elif folder_id == "fld-colab":
                        payload = [
                            {
                                "id": "file-status",
                                "name": "status.json",
                                "mimeType": "application/json",
                                "size": "120",
                                "modifiedTime": "2026-11-19T06:27:14.364Z",
                            },
                            {
                                "id": "fld-mbox",
                                "name": "mailbox",
                                "mimeType": "application/vnd.google-apps.folder",
                                "modifiedTime": "2026-11-18T10:00:00Z",
                            },
                        ]
                    elif folder_id == "fld-mbox":
                        payload = [
                            {
                                "id": "fld-done",
                                "name": "done",
                                "mimeType": "application/vnd.google-apps.folder",
                                "modifiedTime": "2026-11-18T10:00:00Z",
                            }
                        ]
                    elif folder_id == "fld-done":
                        payload = [
                            {
                                "id": "file-job",
                                "name": "job1.tts.json",
                                "mimeType": "application/json",
                                "size": "345",
                                "modifiedTime": "2026-11-20T01:02:03Z",
                            }
                        ]
                    else:
                        payload = []
                    return CommandResult(returncode=0, stdout=json.dumps(payload), stderr="")
                if "download" in args:
                    out_idx = args.index("--out") + 1
                    Path(args[out_idx]).write_text('{"status":"ONLINE"}', encoding="utf-8")
                    return CommandResult(returncode=0, stdout="Downloaded 19 B", stderr="")
                return CommandResult(returncode=1, stdout="", stderr="unexpected command")

            gw = GdriveCliDriveGateway(gdrive_bin="/usr/bin/true", id_cache_path=id_cache)
            caps = gw.capabilities()
            self.assertEqual(caps["backend"], "gdrive_cli")
            self.assertFalse(caps["readonly"])
            self.assertFalse(caps["shared_client_id"])
            self.assertEqual(caps["warnings"], [])

            with mock.patch("faiz_dash.gateways.drive.run_exec", side_effect=fake_run_exec):
                items = await gw.lsl("Colab_Notebooks/mailbox/done", max_depth=1)
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["name"], "job1.tts.json")
                self.assertEqual(items[0]["size"], 345)
                self.assertEqual(items[0]["modified_at"], "2026-11-20 01:02:03")

                # Folder IDs for Colab_Notebooks, status.json, mailbox, done are cached now
                calls_before = len(calls)
                status_txt = await gw.cat_text("Colab_Notebooks/status.json")
                self.assertEqual(json.loads(status_txt)["status"], "ONLINE")
                # Only 1 download call needed because file-status was cached during ls!
                self.assertEqual(len(calls) - calls_before, 1)

                quota = await gw.about()
                self.assertGreater(quota["used"], 0)
                self.assertEqual(quota["total"], 5120 * 1024**3)
                self.assertEqual(quota["free"], quota["total"] - quota["used"])

            # Missing fields in quota never raise and return {}
            self.assertEqual(parse_gdrive_quota("{}"), {})
            self.assertEqual(parse_gdrive_quota("Trash: 0 B"), {})

            # Persisted cache reloads cleanly on fresh instance
            gw2 = GdriveCliDriveGateway(gdrive_bin="/usr/bin/true", id_cache_path=id_cache)
            self.assertEqual(gw2._id_cache.get("Colab_Notebooks/mailbox/done"), "fld-done")

            # Factory selection
            self.assertIsInstance(create_drive_gateway("rclone"), RcloneDriveGateway)
            self.assertIsInstance(create_drive_gateway("gdrive"), GdriveCliDriveGateway)
            with mock.patch.object(GdriveCliDriveGateway, "is_configured", return_value=True):
                self.assertIsInstance(create_drive_gateway("auto"), GdriveCliDriveGateway)
            with mock.patch.object(GdriveCliDriveGateway, "is_configured", return_value=False):
                self.assertIsInstance(create_drive_gateway("auto"), RcloneDriveGateway)

    async def test_cloud_probe_control_plane_decoupled_from_drive_failure_and_backoff(self) -> None:
        class FakeColabGateway(ColabCliGateway):
            def __init__(self, sessions: tuple[dict[str, object], ...]) -> None:
                super().__init__(colab_bin="/usr/bin/true")
                self.sessions = sessions
                self.calls = 0

            def is_configured(self) -> bool:
                return True

            async def list_sessions(self) -> tuple[dict[str, object], ...]:
                self.calls += 1
                return self.sessions

        class CountingFailDrive(FakeDriveGateway):
            def __init__(self) -> None:
                super().__init__(should_fail=True)
                self.cat_calls = 0
                self.about_calls = 0

            def capabilities(self) -> dict[str, object]:
                return {
                    "backend": "gdrive_cli",
                    "configured": True,
                    "read": True,
                    "write": True,
                    "readonly": False,
                    "shared_client_id": False,
                    "warnings": [],
                }

            async def cat_text(self, remote_path: str) -> str:
                self.cat_calls += 1
                raise RuntimeError("403 RATE_LIMIT_EXCEEDED: userRateLimitExceeded")

            async def about(self) -> dict[str, object]:
                self.about_calls += 1
                raise RuntimeError("403 RATE_LIMIT_EXCEEDED")

        with tempfile.TemporaryDirectory() as tmp:
            backoff_file = Path(tmp) / "cloud_backoff.json"
            drive = CountingFailDrive()
            colab_gw = FakeColabGateway(
                sessions=(
                    {
                        "name": "faiz-tts",
                        "endpoint": "https://example.colab.googleusercontent.com/",
                        "hardware": "L4",
                        "shape": "STANDARD",
                        "variant": "GPU",
                    },
                )
            )
            probe = CloudProbe(drive, colab_gateway=colab_gw, backoff_path=backoff_file)

            gce_calls = 0

            async def fake_gce() -> tuple[tuple[GceInstanceInfo, ...], None]:
                nonlocal gce_calls
                gce_calls += 1
                return (GceInstanceInfo("spot-l4", "us-central1-a", "TERMINATED"),), None

            probe._fetch_gce_instances = fake_gce  # type: ignore[method-assign]

            # 1. Active Colab session overrides failing Drive -> ONLINE (NOT DEGRADED)
            t0 = datetime(2026, 11, 23, 8, 0, 0, tzinfo=timezone.utc)
            snap_online = await probe.collect(now_utc=t0)
            self.assertEqual(snap_online.colab_state, "ONLINE")
            self.assertEqual(snap_online.colab_sessions_count, 1)
            self.assertEqual(snap_online.colab_gpu, "L4")
            self.assertEqual(snap_online.colab_source, "colab_cli")
            self.assertEqual(snap_online.drive_backend, "gdrive")
            self.assertFalse(snap_online.rclone_shared_client_id)
            self.assertFalse(snap_online.stale)
            self.assertEqual(drive.cat_calls, 1)
            self.assertEqual(colab_gw.calls, 1)
            self.assertEqual(gce_calls, 1)

            # 2. During Drive backoff cooldown, Drive is skipped, but colab_gateway + GCE still probe!
            colab_gw.sessions = ()
            t1 = datetime(2026, 11, 23, 8, 1, 0, tzinfo=timezone.utc)
            snap_stopped = await probe.collect(now_utc=t1)
            self.assertEqual(drive.cat_calls, 1)  # Skipped by backoff!
            self.assertEqual(colab_gw.calls, 2)   # Still probed!
            self.assertEqual(gce_calls, 2)        # Still probed!
            self.assertEqual(snap_stopped.colab_state, "STOPPED")
            self.assertEqual(snap_stopped.colab_sessions_count, 0)
            self.assertFalse(snap_stopped.stale)
            self.assertEqual(len(snap_stopped.gce_instances), 1)


if __name__ == "__main__":
    unittest.main()
