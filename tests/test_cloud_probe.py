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

        probe = CloudProbe(
            UnconfiguredDrive(),
            gcloud_bin="/nonexistent/gcloud",
            gcp_project="your-gcp-project",
            adc_path=Path("/nonexistent/adc.json"),
        )
        snap = await probe.collect()
        self.assertEqual(snap.colab_state, "NOT_CONFIGURED")
        self.assertIsNone(snap.error)
        self.assertFalse(snap.stale)

    async def test_pure_gcp_gce_without_colab_or_drive(self) -> None:
        class UnconfiguredDrive(FakeDriveGateway):
            def capabilities(self) -> dict[str, object]:
                return {
                    "configured": False,
                    "backend": "rclone",
                    "read": False,
                    "write": False,
                    "readonly": False,
                    "shared_client_id": False,
                    "warnings": [],
                }

        with tempfile.TemporaryDirectory() as tmp:
            adc_file = Path(tmp) / "adc.json"
            adc_file.write_text(
                json.dumps({
                    "type": "authorized_user",
                    "client_id": "cid",
                    "client_secret": "csec",
                    "refresh_token": "rt",
                    "quota_project_id": "pure-gcp-proj",
                }),
                encoding="utf-8",
            )
            probe = CloudProbe(
                UnconfiguredDrive(),
                gcloud_bin="",
                gcp_project="your-gcp-project",
                cache_dir=Path(tmp),
                adc_path=adc_file,
            )

            async def fake_gce() -> tuple[tuple[GceInstanceInfo, ...], None]:
                return (
                    GceInstanceInfo(
                        name="spot-only",
                        zone="us-central1-a",
                        status="RUNNING",
                        machine_type="n2-standard-4",
                        created_at="2026-11-26T06:00:00Z",
                        provisioning_model="SPOT",
                    ),
                ), None

            probe._fetch_gce_instances = fake_gce  # type: ignore[method-assign]
            snap = await probe.collect()
            self.assertEqual(snap.colab_state, "NOT_CONFIGURED")
            self.assertEqual(snap.project, "pure-gcp-proj")
            self.assertEqual(len(snap.gce_instances), 1)
            self.assertEqual(snap.gce_instances[0].name, "spot-only")
            self.assertFalse(snap.stale)
            self.assertIsNone(snap.error)

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

    async def test_gce_rest_api_fields_pagination_401_retry_and_secret_token_cache(self) -> None:
        import io
        import urllib.error
        import urllib.parse

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            cache_dir.mkdir()
            adc_file = root / "adc.json"
            adc_file.write_text(
                json.dumps({
                    "type": "authorized_user",
                    "client_id": "cid-123",
                    "client_secret": "csec-xyz",
                    "refresh_token": "rt-secret",
                    "quota_project_id": "faiz-gpu-engine",
                }),
                encoding="utf-8",
            )
            probe = CloudProbe(
                FakeDriveGateway(status_payload={"status": "STOPPED"}),
                gcloud_bin="",
                gcp_project="faiz-gpu-engine",
                backoff_path=cache_dir / "cloud_backoff.json",
                cache_dir=cache_dir,
                adc_path=adc_file,
            )

            seen_urls: list[str] = []
            oauth_calls = 0
            gce_calls = 0

            class _FakeResp:
                def __init__(self, payload: dict[str, object]) -> None:
                    self._raw = json.dumps(payload).encode("utf-8")

                def read(self) -> bytes:
                    return self._raw

                def __enter__(self) -> "_FakeResp":
                    return self

                def __exit__(self, *args: object) -> None:
                    return None

            def fake_urlopen(req: object, timeout: float = 0.0) -> _FakeResp:
                nonlocal oauth_calls, gce_calls
                url = getattr(req, "full_url", "")
                if "oauth2.googleapis.com/token" in url:
                    oauth_calls += 1
                    return _FakeResp({"access_token": f"ya29.real_token_{oauth_calls}", "expires_in": 3600})
                seen_urls.append(url)
                gce_calls += 1
                if gce_calls == 1:
                    raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b""))  # type: ignore[arg-type]
                if "pageToken=" not in url:
                    return _FakeResp({
                        "nextPageToken": "p2",
                        "items": {
                            "zones/us-central1-a": {
                                "instances": [{
                                    "name": "spot-worker-1",
                                    "zone": "https://www.googleapis.com/compute/v1/projects/faiz-gpu-engine/zones/us-central1-a",
                                    "machineType": "https://www.googleapis.com/compute/v1/projects/faiz-gpu-engine/machineTypes/n2-highcpu-4",
                                    "creationTimestamp": "2026-11-26T06:00:00.123-07:00",
                                    "status": "RUNNING",
                                    "scheduling": {"provisioningModel": "SPOT"},
                                }]
                            },
                            "zones/us-east1-b": {"warning": {"code": "NO_RESULTS_ON_PAGE"}},
                        },
                    })
                return _FakeResp({"items": {}})

            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                instances, err = await probe._fetch_gce_instances()

            self.assertIsNone(err)
            self.assertEqual(oauth_calls, 2)  # initial + 401 refresh retry
            self.assertEqual(len(instances), 1)
            self.assertEqual(instances[0].name, "spot-worker-1")
            self.assertEqual(instances[0].zone, "us-central1-a")
            self.assertEqual(instances[0].machine_type, "n2-highcpu-4")
            self.assertEqual(instances[0].provisioning_model, "SPOT")

            qs = urllib.parse.parse_qs(urllib.parse.urlparse(seen_urls[0]).query)
            self.assertEqual(qs.get("returnPartialSuccess"), ["true"])
            self.assertIn("items/*/instances(", qs["fields"][0])
            self.assertIn("scheduling/provisioningModel", qs["fields"][0])

            tok_file = cache_dir / ".gcp_adc_token.json"
            self.assertTrue(tok_file.exists())
            self.assertEqual(tok_file.stat().st_mode & 0o777, 0o600)
            tok_data = json.loads(tok_file.read_text(encoding="utf-8"))
            self.assertEqual(tok_data["access_token"], "ya29.real_token_2")  # NOT redacted to ya29.***

    async def test_colab_get_usage_isolation_preserves_online_and_cached_cu(self) -> None:
        class UsageGateway(ColabCliGateway):
            def __init__(self) -> None:
                super().__init__(colab_bin="/usr/bin/true")
                self.fail_usage = False

            def is_configured(self) -> bool:
                return True

            async def list_sessions(self) -> tuple[dict[str, object], ...]:
                return ({"name": "w1", "endpoint": "https://x", "hardware": "L4"},)

            async def get_usage(self) -> dict[str, object]:
                if self.fail_usage:
                    raise TimeoutError("colab usage timed out")
                return {"balance_cu": 321.5, "usage_rate": "1.95/hr"}

        with tempfile.TemporaryDirectory() as tmp:
            gw = UsageGateway()
            probe = CloudProbe(
                FakeDriveGateway(status_payload={"status": "ONLINE", "idle_seconds": 100, "updated_at": "2026-11-26T08:00:00Z"}),
                colab_gateway=gw,
                colab_usage_ttl_sec=0.0,
                backoff_path=Path(tmp) / "cloud_backoff.json",
                cache_dir=Path(tmp),
            )

            async def no_gce() -> tuple[tuple[GceInstanceInfo, ...], None]:
                return (), None

            probe._fetch_gce_instances = no_gce  # type: ignore[method-assign]
            t0 = datetime(2026, 11, 26, 8, 1, 0, tzinfo=timezone.utc)
            snap1 = await probe.collect(now_utc=t0)
            self.assertEqual(snap1.colab_state, "ONLINE")
            self.assertAlmostEqual(snap1.colab_balance_cu or 0.0, 321.5)
            self.assertEqual(snap1.colab_usage_rate, "1.95/hr")
            self.assertIsNone(snap1.error)

            # Failing get_usage must NOT degrade ONLINE or wipe cached CU or pollute snap.error
            gw.fail_usage = True
            snap2 = await probe.collect(now_utc=t0)
            self.assertEqual(snap2.colab_state, "ONLINE")
            self.assertAlmostEqual(snap2.colab_balance_cu or 0.0, 321.5)
            self.assertIsNone(snap2.error)


if __name__ == "__main__":
    unittest.main()
