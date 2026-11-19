"""Unit tests for usage_probe, transcript incremental burn, channels, and secret redaction."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faiz_dash.collectors.usage_probe import (
    collect_transcript_burn,
    parse_ps_eww_channels,
    parse_usage_text,
    run_pty_usage_probe,
)
from faiz_dash.models import redact_secrets

SAMPLE_USAGE_OUTPUT = """
\x1b[2J\x1b[H└ Models & Quota
  Account: user@example.com

GEMINI MODELS
  Models within this group: Gemini 4 Hydrogen, Gemini 3.8 Flash

  Weekly Limit Remaining
[█████████████████████████████████████████████░░░░░] 90.73%
Refreshes in 165h 24m

  Five Hour Limit Remaining
[███████████████████████████████████░░░░░░░░░░░░░░░] 69.09%
Refreshes in 2h 24m


CLAUDE AND GPT MODELS
  Models within this group: Claude Opus 5.5, Claude Sonnet 5.5, GPT-OSS 120B

  Weekly Limit Remaining
[██████████████████████████████████████████████████] 100.00%
Quota available

  Five Hour Limit Remaining
[██████████████████████████████████████████████████] 100.00%
Quota available


GEMINI 4 ARGON
  For paid Pro plans, Argon quota is separate.

  Gemini 4 Argon Weekly Limit
[███████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░] 13.78%
Refreshes in 164h 41m

  │Within each group, models share a weekly limit and a 5-hour limit.
"""


class TestUsageProbe(unittest.TestCase):
    def test_parse_usage_text_real_sample(self) -> None:
        parsed = parse_usage_text(SAMPLE_USAGE_OUTPUT)
        self.assertEqual(parsed["account"], "user@example.com")
        buckets = {b["group_name"]: b for b in parsed["buckets"]}
        self.assertIn("GEMINI MODELS", buckets)
        self.assertIn("CLAUDE AND GPT MODELS", buckets)
        self.assertIn("GEMINI 4 ARGON", buckets)

        gem = buckets["GEMINI MODELS"]
        self.assertAlmostEqual(gem["weekly_pct"], 90.73)
        self.assertEqual(gem["weekly_refresh"], "Refreshes in 165h 24m")
        self.assertAlmostEqual(gem["five_hour_pct"], 69.09)
        self.assertEqual(gem["five_hour_refresh"], "Refreshes in 2h 24m")
        self.assertEqual(gem["models"], ["Gemini 4 Hydrogen", "Gemini 3.8 Flash"])

        cla = buckets["CLAUDE AND GPT MODELS"]
        self.assertAlmostEqual(cla["weekly_pct"], 100.0)
        self.assertEqual(cla["weekly_refresh"], "Quota available")
        self.assertAlmostEqual(cla["five_hour_pct"], 100.0)

        arg = buckets["GEMINI 4 ARGON"]
        self.assertAlmostEqual(arg["weekly_pct"], 13.78)
        self.assertEqual(arg["weekly_refresh"], "Refreshes in 164h 41m")

    def test_parse_usage_text_prefers_last_repainted_frame(self) -> None:
        second_frame = (
            SAMPLE_USAGE_OUTPUT.replace("90.73%", "51.66%")
            .replace("69.09%", "21.36%")
            .replace("165h 24m", "160h 30m")
            .replace("2h 24m", "2h 30m")
        )
        repainted = SAMPLE_USAGE_OUTPUT + "\n" + second_frame
        parsed = parse_usage_text(repainted)
        buckets = {b["group_name"]: b for b in parsed["buckets"]}
        gem = buckets["GEMINI MODELS"]
        self.assertAlmostEqual(gem["weekly_pct"], 51.66)
        self.assertAlmostEqual(gem["five_hour_pct"], 21.36)
        self.assertEqual(gem["five_hour_refresh"], "Refreshes in 2h 30m")

    def test_parse_usage_text_applies_csi_cursor_diff_repaint(self) -> None:
        # Simulate agy's incremental CSI cursor-positioned RPC update after Frame 1,
        # including updating digits before an existing '%' without re-emitting '%' or headers.
        diff_update = (
            "\x1b[8;54H20.37"
            "\n\x1b[1G\x1b[KRefreshes in 145h 44m"
            "\x1b[12;54H8.23% "
            "\x1b[13;1H\x1b[KRefreshes in 3h 14m"
        )
        parsed = parse_usage_text(SAMPLE_USAGE_OUTPUT + diff_update)
        buckets = {b["group_name"]: b for b in parsed["buckets"]}
        gem = buckets["GEMINI MODELS"]
        self.assertAlmostEqual(gem["weekly_pct"], 20.37)
        self.assertEqual(gem["weekly_refresh"], "Refreshes in 145h 44m")
        self.assertAlmostEqual(gem["five_hour_pct"], 8.23)
        self.assertEqual(gem["five_hour_refresh"], "Refreshes in 3h 14m")
        self.assertAlmostEqual(buckets["GEMINI 4 ARGON"]["weekly_pct"], 13.78)

    def test_parse_ps_eww_channels(self) -> None:
        ps_text = "\n".join(
            [
                "101 /usr/local/bin/agy --dangerously-skip-permissions SHELL=/bin/zsh",
                "102 /usr/local/bin/agy --model gemini-4-hydrogen-medium AGY_ADC_AUTH=1 USER=dev",
                "103 /usr/local/bin/agy FAIZ_NO_DASH=1 TERM=xterm-256color",
                "104 /bin/zsh -c agy",
            ]
        )
        pro, gcp = parse_ps_eww_channels(ps_text)
        self.assertEqual(pro, 1)
        self.assertEqual(gcp, 1)

    def test_incremental_transcript_byte_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "conversation_summaries.db"
            brain_dir = root / "brain"
            offsets_path = root / "dash_offsets.json"
            conv_id = "conv-123"
            t_dir = brain_dir / conv_id / ".system_generated" / "logs"
            t_dir.mkdir(parents=True)
            t_file = t_dir / "transcript.jsonl"

            now = datetime.now(timezone.utc)
            ts_recent = now.isoformat().replace("+00:00", "Z")

            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE conversation_summaries "
                "(conversation_id TEXT PRIMARY KEY, last_modified_time TEXT, not_fully_idle INTEGER)"
            )
            conn.execute(
                "INSERT INTO conversation_summaries VALUES (?, ?, ?)",
                (conv_id, ts_recent, 1),
            )
            conn.commit()
            conn.close()

            line1 = json.dumps(
                {
                    "created_at": ts_recent,
                    "model": "gemini-4-hydrogen-high",
                    "input_tokens": 1000,
                    "cache_read_tokens": 800,
                    "output_tokens": 200,
                }
            )
            t_file.write_text(line1 + "\n", encoding="utf-8")

            burn1 = collect_transcript_burn(
                db_path=db_path,
                brain_dir=brain_dir,
                offsets_path=offsets_path,
                now_epoch=now.timestamp(),
            )
            self.assertEqual(burn1.tokens_1h, 1200)
            self.assertEqual(burn1.tokens_24h, 1200)
            self.assertAlmostEqual(burn1.cache_hit_rate, round(800 / 1800, 4))
            self.assertEqual(burn1.active_sessions, 1)

            saved_offset_1 = json.loads(offsets_path.read_text(encoding="utf-8"))[conv_id]["byte_offset"]
            self.assertEqual(saved_offset_1, t_file.stat().st_size)

            # Append second line -> only new bytes parsed, totals accumulate
            line2 = json.dumps(
                {
                    "created_at": ts_recent,
                    "model": "gemini-4-argon-high",
                    "input_tokens": 1000,
                    "cache_read_tokens": 200,
                    "output_tokens": 300,
                }
            )
            with t_file.open("a", encoding="utf-8") as fh:
                fh.write(line2 + "\n")

            burn2 = collect_transcript_burn(
                db_path=db_path,
                brain_dir=brain_dir,
                offsets_path=offsets_path,
                now_epoch=now.timestamp(),
            )
            self.assertEqual(burn2.tokens_1h, 2500)
            self.assertEqual(burn2.tokens_24h, 2500)
            self.assertAlmostEqual(burn2.cache_hit_rate, round(1000 / 3000, 4))
            self.assertEqual(burn2.by_model_24h["gemini-4-hydrogen-high"], 1200)
            self.assertEqual(burn2.by_model_24h["gemini-4-argon-high"], 1300)

    def test_pty_cooldown_reuses_fresh_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_path = root / "usage_snapshot.json"
            lock_path = root / "usage_probe.lock"
            cache_path.write_text(
                json.dumps(
                    {
                        "account": "cached@example.com",
                        "buckets": [{"group_name": "GEMINI MODELS", "weekly_pct": 88.0}],
                    }
                ),
                encoding="utf-8",
            )
            res = run_pty_usage_probe(
                cache_path=cache_path,
                lock_path=lock_path,
                agy_bin="/nonexistent/agy",
                cooldown_sec=120.0,
                force=False,
            )
            self.assertTrue(res.get("from_cache"))
            self.assertEqual(res["account"], "cached@example.com")

    def test_redact_secrets(self) -> None:
        raw = "key=sk-abc1234567890 google=AIzaSyDummyKey123456789 auth=Bearer tok_secret_xyz123"
        redacted = redact_secrets(raw)
        self.assertNotIn("sk-abc1234567890", redacted)
        self.assertNotIn("AIzaSyDummyKey123456789", redacted)
        self.assertNotIn("tok_secret_xyz123", redacted)
        self.assertIn("sk-***", redacted)
        self.assertIn("AIzaSy***", redacted)
        self.assertIn("Bearer ***", redacted)


if __name__ == "__main__":
    unittest.main()
