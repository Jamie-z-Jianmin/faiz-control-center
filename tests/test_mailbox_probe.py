"""Unit tests for MailboxProbe queue scanning and ComfyUI/TTS anti-false-done verification."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faiz_dash.collectors.mailbox_probe import MailboxProbe


class MockMailboxDrive:
    def __init__(self, lsl_map: dict[str, list[dict]], text_map: dict[str, str]) -> None:
        self.lsl_map = lsl_map
        self.text_map = text_map

    def capabilities(self) -> dict[str, object]:
        return {"read": True, "write": False}

    async def cat_text(self, remote_path: str) -> str:
        if remote_path not in self.text_map:
            raise FileNotFoundError(remote_path)
        return self.text_map[remote_path]

    async def lsf(self, remote_path: str, dirs_only: bool = False) -> list[str]:
        return []

    async def lsl(self, remote_path: str, max_depth: int = 1) -> list[dict]:
        return list(self.lsl_map.get(remote_path, []))

    async def about(self) -> dict[str, object]:
        return {}


class TestMailboxProbe(unittest.IsolatedAsyncioTestCase):
    async def test_comfy_false_done_and_tts_receipt_verification(self) -> None:
        lsl_map = {
            "Colab_Notebooks/mailbox/tasks": [
                {"name": "queued.json", "size": 500, "modified_at": "2026-11-18 18:00:00", "mtime_epoch": 2000.0},
            ],
            "Colab_Notebooks/mailbox/failed": [
                {"name": "bad_job.tts.json", "size": 400, "modified_at": "2026-11-18 17:00:00", "mtime_epoch": 1500.0},
                {"name": "bad_job.tts.json.log", "size": 200, "modified_at": "2026-11-18 17:01:00", "mtime_epoch": 1501.0},
            ],
            "Colab_Notebooks/mailbox/done": [
                {"name": "tts_ok.tts.json", "size": 300, "modified_at": "2026-11-18 18:05:00", "mtime_epoch": 2005.0},
                {"name": "comfy_real.json", "size": 300, "modified_at": "2026-11-18 18:04:00", "mtime_epoch": 2004.0},
                {"name": "comfy_fake.json", "size": 300, "modified_at": "2026-11-18 18:03:00", "mtime_epoch": 2003.0},
                {"name": "comfy_http.json", "size": 300, "modified_at": "2026-11-18 18:02:00", "mtime_epoch": 2002.0},
            ],
            "Projects/proj_a/03_Audio/cloud_raw/tts_ok": [
                {"name": "tts_ok_receipt.json", "size": 1200, "modified_at": "2026-11-18 18:05:10", "mtime_epoch": 2010.0},
                {"name": "line_01.wav", "size": 50000, "modified_at": "2026-11-18 18:05:10", "mtime_epoch": 2010.0},
            ],
            "Projects/proj_real/03_Footage": [
                {"name": "shot_01.mp4", "size": 250000, "modified_at": "2026-11-18 18:04:30", "mtime_epoch": 2010.0},
            ],
            "Projects/proj_fake/03_Footage": [
                {"name": "empty.mp4", "size": 0, "modified_at": "2026-11-18 18:03:30", "mtime_epoch": 2010.0},
            ],
        }
        text_map = {
            "Colab_Notebooks/mailbox/failed/bad_job.tts.json.log": "line1\nAuth failed Bearer secret_tok_999\nExit 1",
            "Colab_Notebooks/mailbox/done/tts_ok.tts.json": json.dumps({"job_id": "tts_ok", "project_name": "proj_a"}),
            "Colab_Notebooks/mailbox/done/comfy_real.json": json.dumps({"project_name": "proj_real"}),
            "Colab_Notebooks/mailbox/done/comfy_fake.json": json.dumps({"project_name": "proj_fake"}),
            "Colab_Notebooks/mailbox/done/comfy_http.json": json.dumps({"project_name": "proj_http", "prompt_id": "pid-999"}),
        }

        def fake_http(url: str, timeout: float) -> dict:
            if "pid-999" in url:
                return {"pid-999": {"status": {"completed": True}}}
            return {}

        probe = MailboxProbe(
            MockMailboxDrive(lsl_map, text_map),
            projects_root="Projects",
            http_get_json=fake_http,
        )
        snap = await probe.collect(
            default_project="proj_default",
            colab_online=True,
            webui_url="https://comfy.example",
            verify_limit=5,
        )

        self.assertEqual(snap.tasks_count, 1)
        self.assertEqual(snap.done_count, 4)
        self.assertEqual(snap.failed_count, 1)
        self.assertNotIn("secret_tok_999", snap.failed_items[0].log_tail)
        self.assertIn("Bearer ***", snap.failed_items[0].log_tail)

        by_name = {item.name: item for item in snap.done_items}
        self.assertTrue(by_name["tts_ok.tts.json"].verified)
        self.assertTrue(by_name["comfy_real.json"].verified)
        self.assertFalse(by_name["comfy_fake.json"].verified)
        self.assertIn("UNVERIFIED_OR_FAILED", by_name["comfy_fake.json"].verify_detail)
        self.assertTrue(by_name["comfy_http.json"].verified)


if __name__ == "__main__":
    unittest.main()
