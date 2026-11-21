"""Integration tests for statusline.sh rendering/latency and _faiz_dash_hook.sh gating."""

from __future__ import annotations

import json
import os
from pathlib import Path
import pty
import re
import select
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
STATUSLINE_SH = REPO_ROOT / "scripts" / "statusline.sh"
HOOK_SH = REPO_ROOT / "bin" / "_faiz_dash_hook.sh"
FAIZ_DASH_SH = REPO_ROOT / "bin" / "faiz-dash"


class TestShellScripts(unittest.TestCase):
    def test_statusline_renders_cached_capsules_and_latency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            cache_dir = fake_home / ".agents" / "cache"
            cache_dir.mkdir(parents=True)
            (cache_dir / "usage_snapshot.json").write_text(
                json.dumps(
                    {
                        "pro_5h_pct": 57.42,
                        "pro_wk_pct": 87.55,
                        "argon_wk_pct": 13.78,
                        "quota_gate": "THROTTLE_ARGON",
                        "pro_count": 2,
                        "gcp_count": 1,
                        "tokens_1h": 1766049,
                        "pro_5h_reset_at_epoch": int(time.time() + 12750),
                    }
                ),
                encoding="utf-8",
            )
            (cache_dir / "cloud_snapshot.json").write_text(
                json.dumps(
                    {
                        "colab_state": "ONLINE",
                        "idle_remaining_seconds": 2685,
                        "gce_running_count": 0,
                        "mailbox_tasks_count": 2,
                        "mailbox_failed_count": 1,
                        "mailbox_unverified_count": 0,
                    }
                ),
                encoding="utf-8",
            )
            t_file = fake_home / "transcript.jsonl"
            now_ep = time.time()
            t_s = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ep - 10))
            t_e = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ep - 5))
            t_file.write_text(
                "\n".join(
                    [
                        json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": t_s, "input_tokens": 3506, "cache_read_tokens": 232915, "output_tokens": 900}),
                        json.dumps({"step_index": 2, "type": "GENERIC", "status": "DONE", "created_at": t_e}),
                    ]
                ) + "\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["HOME"] = str(fake_home)
            env["FAIZ_STATUSLINE_NO_BG"] = "1"
            env["FAIZ_TRANSCRIPT_PATH"] = str(t_file)
            env["FAIZ_TERM_WIDTH"] = "190"
            env["TERM"] = "xterm-kitty"
            env["COLORTERM"] = "truecolor"
            payload = json.dumps(
                {
                    "model": {"display_name": "Gemini 4 Hydrogen (High)"},
                    "context_window": {"used_percentage": 32.4},
                }
            ).encode("utf-8")

            # Warm run timing on wide terminal (W1-A default auto: unified 2x4 bento matrix + equalized column expansion)
            subprocess.run([str(STATUSLINE_SH)], input=payload, env=env, capture_output=True, check=True)
            t0 = time.perf_counter()
            res = subprocess.run([str(STATUSLINE_SH)], input=payload, env=env, capture_output=True, check=True)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0

            raw_out = res.stdout.decode("utf-8", errors="replace")
            out = raw_out.strip()
            ansi_lines = out.splitlines()
            self.assertEqual(len(ansi_lines), 4)
            self.assertIn("─", ansi_lines[0])
            self.assertIn("╌", ansi_lines[2])
            plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
            plain_lines = plain.splitlines()
            self.assertEqual(len({len(ln) for ln in plain_lines}), 1)
            self.assertEqual(len(plain_lines[1]), 188)
            self.assertEqual(plain.count("Context "), 1)
            self.assertIn("\ue0b6", out)
            self.assertIn("\ue0b4", out)
            self.assertIn("35;45;64", out)       # Unified deep slate-blue dashboard glass
            self.assertIn("138;180;248", out)     # Row 1 accent
            self.assertIn("156;172;198", out)     # Row 1 harmonized label tint
            self.assertIn("238;243;252", out)     # Row 1 crisp value
            self.assertNotIn("27;52;38", out)     # No Row 2 frosted green in unified 2x4 matrix
            self.assertIn("\x1b[0m", out)
            self.assertIn("◔ Context 32% ■■□□□□", plain_lines[1])
            self.assertIn("● Cache 98.5% · Live", plain_lines[1])
            self.assertIn("▸ Speed 180 tok/s", plain_lines[1])
            self.assertIn("◑ Gemini 5h:57.42%(↻", plain_lines[1])
            self.assertIn(") · Wk:87.55%", plain_lines[1])
            self.assertIn("▲ Argon 13.78% Low", plain_lines[3])
            self.assertIn("◆ Session 4.4K · 1h:1.8M", plain_lines[3])
            self.assertIn("● Colab ONLINE · 44m left", plain_lines[3])
            self.assertIn("▲ Queue 2 Run · 1 Fail", plain_lines[3])
            self.assertLess(elapsed_ms, 80.0)

            # 91-col split with density="auto": 4-col aligned frosted matrix + faint top/mid hairline dividers
            env_91 = {**env, "FAIZ_TERM_WIDTH": "91"}
            res_91 = subprocess.run([str(STATUSLINE_SH)], input=payload, env=env_91, capture_output=True, check=True)
            out_91 = res_91.stdout.decode("utf-8", errors="replace")
            ansi_lines_91 = out_91.splitlines()
            self.assertEqual(len(ansi_lines_91), 4)
            self.assertIn("─", ansi_lines_91[0])
            self.assertIn("35;45;64", ansi_lines_91[1])
            self.assertIn("╌", ansi_lines_91[2])
            self.assertIn("35;45;64", ansi_lines_91[3])
            self.assertNotIn("27;52;38", out_91)  # Unified slate-blue matrix surface on both rows
            plain_91 = re.sub(r"\x1b\[[0-9;]*m", "", out_91)
            plain_lines_91 = plain_91.splitlines()
            self.assertEqual(len({len(ln) for ln in plain_lines_91}), 1)
            self.assertLessEqual(len(plain_lines_91[1]), 89)
            self.assertIn("Cache 99%", plain_lines_91[1])
            self.assertIn("Speed 180t/s", plain_lines_91[1])
            self.assertIn("Gemini 57.42%·87.55%", plain_lines_91[1])
            self.assertIn("Argon 13.78%!", plain_lines_91[3])
            self.assertIn("◆ Burn 4.4K", plain_lines_91[3])
            self.assertIn("Colab ON(44m)", plain_lines_91[3])
            self.assertIn("Queue 2R · 1F", plain_lines_91[3])
            self.assertNotIn("live", plain_91)

            # Switch secondary_metric="reset" & idle queue -> displays Reset countdown and Queue Idle (suppresses UNV noise)
            (fake_home / ".agents" / "statusline_config.json").write_text(
                json.dumps({"secondary_metric": "reset"}), encoding="utf-8"
            )
            (cache_dir / "cloud_snapshot.json").write_text(
                json.dumps({"colab_state": "ONLINE", "idle_remaining_seconds": 2685, "mailbox_tasks_count": 0, "mailbox_failed_count": 0, "mailbox_unverified_count": 3}),
                encoding="utf-8",
            )
            res_rst = subprocess.run([str(STATUSLINE_SH)], input=payload, env=env_91, capture_output=True, check=True)
            plain_rst = re.sub(r"\x1b\[[0-9;]*m", "", res_rst.stdout.decode("utf-8", errors="replace"))
            self.assertIn("↻ Reset 3h3", plain_rst)
            self.assertIn("Queue Idle", plain_rst)
            self.assertNotIn("UNV", plain_rst)

            # Restore mailbox fixture for subsequent expanded check
            (cache_dir / "cloud_snapshot.json").write_text(
                json.dumps({"colab_state": "ONLINE", "idle_remaining_seconds": 2685, "gce_running_count": 0, "mailbox_tasks_count": 2, "mailbox_failed_count": 1, "mailbox_unverified_count": 0}),
                encoding="utf-8",
            )

            # Explicit density="compact" on 91-col split packs into 1 single compact row (+ leading top_gap)
            (fake_home / ".agents" / "statusline_config.json").write_text(
                json.dumps({"density": "compact"}), encoding="utf-8"
            )
            res_compact = subprocess.run([str(STATUSLINE_SH)], input=payload, env=env_91, capture_output=True, check=True)
            raw_compact = res_compact.stdout.decode("utf-8", errors="replace")
            self.assertTrue(raw_compact.startswith("\n"))
            plain_compact = re.sub(r"\x1b\[[0-9;]*m", "", raw_compact.strip())
            self.assertNotIn("\n", plain_compact)
            self.assertIn("Ctx 32%·C99%·180t/s", plain_compact)

            # Reset config to default density="auto" for narrow 4x2 checks
            (fake_home / ".agents" / "statusline_config.json").write_text("{}", encoding="utf-8")

            # Explicit 58-col narrow split (FAIZ_TERM_WIDTH=60) with density="auto": strict 4x2 matrix, uniform slate-blue
            env_60 = {**env, "FAIZ_TERM_WIDTH": "60"}
            res_60 = subprocess.run([str(STATUSLINE_SH)], input=payload, env=env_60, capture_output=True, check=True)
            out_60 = res_60.stdout.decode("utf-8", errors="replace")
            ansi_lines_60 = out_60.splitlines()
            self.assertEqual(len(ansi_lines_60), 6)
            self.assertIn("─", ansi_lines_60[0])
            self.assertIn("╌", ansi_lines_60[3])
            self.assertIn("35;45;64", out_60)
            self.assertNotIn("27;52;38", out_60)
            self.assertNotIn("60;50;26", out_60)
            self.assertNotIn("64;30;31", out_60)
            plain_60 = re.sub(r"\x1b\[[0-9;]*m", "", out_60).splitlines()
            self.assertEqual(len({len(ln) for ln in plain_60}), 1)
            self.assertEqual(len(plain_60[1]), 58)
            self.assertIn("▸ 180t/s", plain_60[2])
            self.assertIn("Gemini 57%·88%", plain_60[2])

            # 46-col narrow Rex sidebar (FAIZ_TERM_WIDTH=46): stays strictly 4x2 aligned and uniform slate-blue
            env_46 = {**env, "FAIZ_TERM_WIDTH": "46"}
            res_46 = subprocess.run([str(STATUSLINE_SH)], input=payload, env=env_46, capture_output=True, check=True)
            out_46 = res_46.stdout.decode("utf-8", errors="replace")
            ansi_lines_46 = out_46.splitlines()
            self.assertEqual(len(ansi_lines_46), 6)
            self.assertNotIn("27;52;38", out_46)
            plain_46 = re.sub(r"\x1b\[[0-9;]*m", "", out_46).splitlines()
            self.assertEqual(len({len(ln) for ln in plain_46}), 1)
            self.assertLessEqual(len(plain_46[1]), 44)
            self.assertIn("Gem 57%·88%", plain_46[2])

            # Per-capsule independent color override via capsule_colors (and CLI config command)
            cli_env = {**env, "PYTHONPATH": str(REPO_ROOT / "src")}
            subprocess.run(
                [sys.executable, "-m", "faiz_dash", "config", "--capsule-color", "gemini=green", "--capsule-color", "mailbox=red"],
                env=cli_env,
                capture_output=True,
                check=True,
            )
            res_cap = subprocess.run([str(STATUSLINE_SH)], input=payload, env=env_60, capture_output=True, check=True)
            out_cap = res_cap.stdout.decode("utf-8", errors="replace")
            cap_lines = out_cap.splitlines()
            self.assertIn("35;45;64", cap_lines[1])
            self.assertNotIn("27;52;38", cap_lines[1])
            self.assertIn("27;52;38", cap_lines[2])   # Gemini overridden to green
            self.assertIn("64;30;31", cap_lines[5])   # Queue overridden to red

            # Explicit density="expanded" with custom row_colors on 95-col split wraps cleanly onto 2 frosted rows (Blue / Green)
            (fake_home / ".agents" / "statusline_config.json").write_text(
                json.dumps({"density": "expanded", "line_gap": False, "row_colors": ["blue", "green"]}), encoding="utf-8"
            )
            env_95 = {**env, "FAIZ_TERM_WIDTH": "95"}
            res_95 = subprocess.run([str(STATUSLINE_SH)], input=payload, env=env_95, capture_output=True, check=True)
            out_95 = res_95.stdout.decode("utf-8", errors="replace").strip()
            self.assertNotIn("\n\n", out_95)
            parts = out_95.splitlines()
            self.assertEqual(len(parts), 2)
            self.assertIn("35;45;64", parts[0])
            self.assertNotIn("27;52;38", parts[0])
            self.assertIn("27;52;38", parts[1])
            self.assertNotIn("35;45;64", parts[1])
            self.assertIn("2Q·1F", parts[1])

            # Explicit density="expanded" on 190-col wide renders single-row expanded capsules with freshness
            res_exp_wide = subprocess.run([str(STATUSLINE_SH)], input=payload, env=env, capture_output=True, check=True)
            raw_exp_wide = res_exp_wide.stdout.decode("utf-8", errors="replace")
            self.assertTrue(raw_exp_wide.startswith("\n"))
            plain_exp_wide = re.sub(r"\x1b\[[0-9;]*m", "", raw_exp_wide.strip())
            self.assertNotIn("\n", plain_exp_wide)
            self.assertIn("▸ 180t/s", plain_exp_wide)
            self.assertIn("▲ Argon 13.78% (Low)", plain_exp_wide)
            self.assertIn("▲ Queue 2Q·1F", plain_exp_wide)
            self.assertIn("live", plain_exp_wide)

    def test_statusline_custom_config_zh_and_degraded_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            cache_dir = fake_home / ".agents" / "cache"
            cache_dir.mkdir(parents=True)
            (cache_dir / "usage_snapshot.json").write_text(
                json.dumps({"pro_5h_pct": 52.0, "pro_wk_pct": 86.0, "argon_wk_pct": 13.8, "pro_count": 3, "gcp_count": 0}),
                encoding="utf-8",
            )
            (cache_dir / "cloud_snapshot.json").write_text(
                json.dumps(
                    {
                        "colab_state": "DEGRADED",
                        "cloud_ok": False,
                        "cloud_error_kind": "rate_limit",
                        "gce_running_count": 0,
                        "mailbox_tasks_count": 0,
                        "mailbox_failed_count": 1,
                    }
                ),
                encoding="utf-8",
            )
            t_file = fake_home / "transcript.jsonl"
            now_ep = time.time()
            # 250s stale step -> triggers cache cold alert ("冷却") & low speed <10 t/s
            t_s = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ep - 260))
            t_e = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ep - 250))
            t_file.write_text(
                "\n".join(
                    [
                        json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": t_s, "input_tokens": 3506, "cache_read_tokens": 232915, "output_tokens": 60}),
                        json.dumps({"step_index": 2, "type": "GENERIC", "status": "DONE", "created_at": t_e}),
                    ]
                ) + "\n",
                encoding="utf-8",
            )
            cfg_file = fake_home / ".agents" / "statusline_config.json"
            cfg_file.write_text(
                json.dumps(
                    {
                        "language": "zh",
                        "style": "pill",
                        "indicator": "bar",
                        "density": "expanded",
                        "theme": "custom",
                        "color_mode": "semantic",
                        "top_gap": True,
                        "line_gap": True,
                        "auto_wrap": True,
                        "modules": {"context": True, "cache": True, "speed": True, "quota": True, "sessions": False, "colab": True, "mailbox": True, "freshness": True},
                        "colors": {"context": "magenta", "warn": "bright_yellow"},
                    }
                ),
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "HOME": str(fake_home),
                "FAIZ_STATUSLINE_NO_BG": "1",
                "FAIZ_STATUSLINE_CONFIG": str(cfg_file),
                "FAIZ_TRANSCRIPT_PATH": str(t_file),
                "FAIZ_TERM_WIDTH": "180",
                "TERM": "xterm-kitty",
                "COLORTERM": "truecolor",
            }
            res = subprocess.run([str(STATUSLINE_SH)], input=b'{"context_window":{"used_percentage":10}}', env=env, capture_output=True, check=True)
            out = res.stdout.decode("utf-8", errors="replace")
            self.assertTrue(out.startswith("\n"))
            self.assertIn("▎", out)
            self.assertIn("上下文 10% ■□□□□□", out)
            self.assertIn("缓存 98.5% 冷却", out)
            self.assertIn("6t/s", out)
            self.assertIn("180;142;173", out)
            self.assertIn("Wk:86.00%", out)
            self.assertNotIn("会话 ", out)
            self.assertIn("▲ Colab 限流", out)
            self.assertIn("队列 0待·1败", out)
            self.assertIn("rc限流", out)
            self.assertNotIn("实时", out)

    def test_statusline_styles_themes_and_apple_terminal_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            cfg_file = fake_home / ".agents" / "statusline_config.json"
            cfg_file.parent.mkdir(parents=True)
            base_env = {
                **os.environ,
                "HOME": str(fake_home),
                "FAIZ_STATUSLINE_NO_BG": "1",
                "FAIZ_STATUSLINE_CONFIG": str(cfg_file),
                "FAIZ_TRANSCRIPT_PATH": "",
                "FAIZ_TERM_WIDTH": "160",
                "TERM": "xterm-kitty",
                "COLORTERM": "truecolor",
            }

            # Ghost style emits transparent brackets without background sequences
            cfg_file.write_text(json.dumps({"style": "ghost", "theme": "google"}), encoding="utf-8")
            res_gh = subprocess.run([str(STATUSLINE_SH)], input=b'{"context_window":{"used_percentage":40}}', env=base_env, capture_output=True, check=True)
            out_gh = res_gh.stdout.decode("utf-8", errors="replace")
            plain_gh = re.sub(r"\x1b\[[0-9;]*m", "", out_gh)
            self.assertNotIn("\x1b[48;2;", out_gh)
            self.assertIn("107;114;128", out_gh)
            self.assertIn("[◑ Context 40% ■■□□□□]", plain_gh)

            # Powerline style with Google theme uses Material 800 deep shades + crisp white fg
            cfg_file.write_text(json.dumps({"style": "powerline", "theme": "google", "indicator": "bar"}), encoding="utf-8")
            res_pw = subprocess.run([str(STATUSLINE_SH)], input=b'{"context_window":{"used_percentage":40}}', env=base_env, capture_output=True, check=True)
            out_pw = res_pw.stdout.decode("utf-8", errors="replace")
            self.assertIn("\ue0b6", out_pw)
            self.assertIn("\ue0b4", out_pw)
            self.assertIn("23;78;166", out_pw)
            self.assertIn("248;249;250", out_pw)

            # Pill style emits crisp left accent bar + elevated Material 800 Google palette
            cfg_file.write_text(json.dumps({"style": "pill", "theme": "google"}), encoding="utf-8")
            res_pl = subprocess.run([str(STATUSLINE_SH)], input=b'{"context_window":{"used_percentage":40}}', env=base_env, capture_output=True, check=True)
            out_pl = res_pl.stdout.decode("utf-8", errors="replace")
            self.assertIn("▎", out_pl)
            self.assertIn("23;78;166", out_pl)

            # Apple_Terminal forces minimal fallback even when style=ghost and TERM=xterm-256color
            cfg_file.write_text(json.dumps({"style": "ghost", "theme": "google", "indicator": "bar"}), encoding="utf-8")
            apple_env = {**base_env, "TERM_PROGRAM": "Apple_Terminal", "TERM": "xterm-256color", "COLORTERM": ""}
            res_ap = subprocess.run([str(STATUSLINE_SH)], input=b'{"context_window":{"used_percentage":40}}', env=apple_env, capture_output=True, check=True)
            out_ap = res_ap.stdout.decode("utf-8", errors="replace")
            self.assertNotIn("\x1b[48;2;", out_ap)
            self.assertNotIn("[◔", out_ap)
            self.assertIn(" | ", out_ap)
            self.assertIn("Context 40% ■■□□□□", out_ap)

    def test_statusline_broken_stdin_path_falls_back_to_cli_brain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            t_dir = fake_home / ".gemini" / "antigravity-cli" / "brain" / "conv-live" / ".system_generated" / "logs"
            t_dir.mkdir(parents=True)
            t_file = t_dir / "transcript.jsonl"
            now_ep = time.time()
            t_s = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ep - 8))
            t_e = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ep - 4))
            t_file.write_text(
                "\n".join(
                    [
                        json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": t_s, "input_tokens": 500, "cache_read_tokens": 9500, "output_tokens": 400}),
                        json.dumps({"step_index": 2, "type": "GENERIC", "status": "DONE", "created_at": t_e}),
                    ]
                ) + "\n",
                encoding="utf-8",
            )
            env = {**os.environ, "HOME": str(fake_home), "FAIZ_STATUSLINE_NO_BG": "1", "TERM": "xterm-kitty", "COLORTERM": "truecolor", "FAIZ_TERM_WIDTH": "160"}
            env.pop("FAIZ_TRANSCRIPT_PATH", None)
            broken_payload = json.dumps({
                "context_window": {"used_percentage": 22},
                "transcript_path": str(fake_home / ".gemini" / "antigravity" / "brain" / ".system_generated" / "logs" / "transcript.jsonl"),
            }).encode("utf-8")
            res = subprocess.run([str(STATUSLINE_SH)], input=broken_payload, env=env, capture_output=True, check=True)
            plain = re.sub(r"\x1b\[[0-9;]*m", "", res.stdout.decode("utf-8", errors="replace"))
            self.assertIn("Cache 95.0%", plain)
            self.assertIn("100 tok/s", plain)

    def test_statusline_graceful_without_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["HOME"] = tmp
            env["FAIZ_STATUSLINE_NO_BG"] = "1"
            env["FAIZ_TRANSCRIPT_PATH"] = ""
            env["FAIZ_TERM_WIDTH"] = "160"
            env["TERM"] = "xterm-kitty"
            env["COLORTERM"] = "truecolor"
            res = subprocess.run([str(STATUSLINE_SH)], input=b"{}", env=env, capture_output=True, check=True)
            out = res.stdout.decode("utf-8", errors="replace")
            plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
            self.assertIn("Faiz", plain)
            self.assertIn("○ Quota syncing", plain)

    def _run_hook_in_pty(self, args: list[str], env_overrides: dict[str, str], fake_bin: Path) -> None:
        master, slave = pty.openpty()
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
        env.pop("FAIZ_NO_DASH", None)
        env.pop("REX_BLOCK", None)
        env.pop("REX_BLOCK_ID", None)
        env.update(env_overrides)

        cmd_args = " ".join(repr(a) for a in args)
        script = f"source {str(HOOK_SH)!r}; _maybe_launch_faiz_dash {cmd_args}; wait"
        proc = subprocess.Popen(
            ["bash", "-c", script],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            close_fds=True,
        )
        os.close(slave)
        try:
            proc.wait(timeout=5.0)
            while select.select([master], [], [], 0.05)[0]:
                if not os.read(master, 4096):
                    break
        finally:
            os.close(master)

    def test_hook_anti_recursion_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_home = root / "home"
            (fake_home / ".agents" / "cache").mkdir(parents=True)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            rex_log = root / "rex_calls.log"
            fake_rex = fake_bin / "rex"
            fake_rex.write_text(
                f"""#!/usr/bin/env bash
echo "$@" >> {str(rex_log)!r}
case "$1" in
  whoami) echo '{{"window_id":"win-1"}}' ;;
  block) echo '{{"blocks":[]}}' ;;
esac
""",
                encoding="utf-8",
            )
            fake_rex.chmod(0o755)

            base_env = {"HOME": str(fake_home), "REX_BLOCK_ID": "block:123"}

            # 1. All blocked modes must NEVER invoke rex
            blocked_cases = [
                (["-p", "hi"], base_env),
                (["--print", "hi"], base_env),
                (["acp"], base_env),
                (["models"], base_env),
                (["help"], base_env),
                (["update"], base_env),
                (["plugin", "list"], base_env),
                ([], {**base_env, "FAIZ_NO_DASH": "1"}),
                ([], {"HOME": str(fake_home)}),  # missing REX_BLOCK_ID / REX_BLOCK
            ]
            for args, env_ov in blocked_cases:
                self._run_hook_in_pty(args, env_ov, fake_bin)
            self.assertFalse(rex_log.exists(), f"Blocked case triggered rex: {rex_log.read_text() if rex_log.exists() else ''}")

            # 2. dash_disabled must block rex
            disabled_flag = fake_home / ".agents" / "cache" / "dash_disabled"
            disabled_flag.write_text("", encoding="utf-8")
            self._run_hook_in_pty([], base_env, fake_bin)
            self.assertFalse(rex_log.exists())
            disabled_flag.unlink()

            # 3. Valid interactive TTY + REX_BLOCK_ID must trigger rex split
            self._run_hook_in_pty([], base_env, fake_bin)
            self.assertTrue(rex_log.exists())
            calls = rex_log.read_text(encoding="utf-8")
            self.assertIn("split -b block:123 --split=below --ratio 22 --focus=false --label faiz-dash", calls)

            # 4. Existing faiz-dash block in same window_id must NOT split again
            rex_log.unlink()
            fake_rex.write_text(
                f"""#!/usr/bin/env bash
echo "$@" >> {str(rex_log)!r}
case "$1" in
  whoami) echo '{{"window_id":"win-1"}}' ;;
  block) echo '{{"blocks":[{{"block_id":"b9","window_id":"win-1","label":"faiz-dash"}}]}}' ;;
esac
""",
                encoding="utf-8",
            )
            fake_rex.chmod(0o755)
            self._run_hook_in_pty([], base_env, fake_bin)
            calls2 = rex_log.read_text(encoding="utf-8")
            self.assertNotIn("split ", calls2)

    def test_statusline_cloud_backoff_skips_bg_and_detaches_pipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_home = root / "home"
            cache_dir = fake_home / ".agents" / "cache"
            cache_dir.mkdir(parents=True)
            (cache_dir / "usage_snapshot.json").write_text(
                json.dumps({"pro_5h_pct": 80.0, "pro_wk_pct": 70.0, "pty_fetched_epoch": int(time.time())}),
                encoding="utf-8",
            )
            (cache_dir / ".bg_pty_ts").write_text("", encoding="utf-8")
            # Force cloud_snapshot.json mtime stale (>90s) while cloud_backoff.json is active in future
            old_ts = time.time() - 300
            cloud_f = cache_dir / "cloud_snapshot.json"
            cloud_f.write_text(json.dumps({"colab_state": "DEGRADED"}), encoding="utf-8")
            os.utime(cloud_f, (old_ts, old_ts))
            (cache_dir / "cloud_backoff.json").write_text(
                json.dumps({"next_allowed_epoch": time.time() + 240, "error_streak": 2}),
                encoding="utf-8",
            )

            marker = root / "dash_invoked.log"
            fake_dash = root / "fake-dash"
            fake_dash.write_text(
                f"#!/usr/bin/env bash\necho called >> {str(marker)!r}\nsleep 1\n",
                encoding="utf-8",
            )
            fake_dash.chmod(0o755)
            env = {
                **os.environ,
                "HOME": str(fake_home),
                "FAIZ_DASH_BIN": str(fake_dash),
                "FAIZ_TERM_WIDTH": "160",
                "TERM": "xterm-kitty",
                "COLORTERM": "truecolor",
            }
            env.pop("FAIZ_STATUSLINE_NO_BG", None)

            t0 = time.perf_counter()
            res = subprocess.run([str(STATUSLINE_SH)], input=b"{}", env=env, capture_output=True, check=True)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self.assertLess(elapsed_ms, 120.0)
            self.assertFalse(marker.exists())
            self.assertIn("Gemini", res.stdout.decode("utf-8", errors="replace"))

    def test_faiz_dash_side_toggle_and_non_rex_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_home = root / "home"
            (fake_home / ".agents" / "cache").mkdir(parents=True)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            rex_log = root / "rex_calls.log"
            fake_rex = fake_bin / "rex"
            fake_rex.write_text(
                f"""#!/usr/bin/env bash
echo "$@" >> {str(rex_log)!r}
case "$1" in
  whoami) echo '{{"window_id":"win-1"}}' ;;
  block)
    if [ "${{2:-}}" = "list" ]; then
      echo '{{"blocks":[]}}'
    fi
    ;;
esac
""",
                encoding="utf-8",
            )
            fake_rex.chmod(0o755)

            # 1. Non-Rex fallback prints guidance and skips rex
            non_rex_env = {**os.environ, "HOME": str(fake_home), "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"}
            non_rex_env.pop("REX_BLOCK_ID", None)
            non_rex_env.pop("REX_BLOCK", None)
            res_nr = subprocess.run([str(FAIZ_DASH_SH), "side"], env=non_rex_env, capture_output=True, text=True, check=True)
            self.assertIn("not inside a Rex pane", res_nr.stdout)
            self.assertFalse(rex_log.exists())

            # 2. First `faiz-dash side` inside Rex opens right sidebar with preset 2
            rex_env = {**non_rex_env, "REX_BLOCK_ID": "block:123"}
            res_open = subprocess.run([str(FAIZ_DASH_SH), "side"], env=rex_env, capture_output=True, text=True, check=True)
            self.assertIn("opened faiz-dash-side pane", res_open.stdout)
            open_calls = rex_log.read_text(encoding="utf-8")
            self.assertIn("split -b block:123 --split=right --ratio 28 --focus=false --label faiz-dash-side --shell=none", open_calls)
            self.assertIn("--preset 2", open_calls)

            # 3. Second `faiz-dash side` when faiz-dash-side exists closes it
            rex_log.unlink()
            fake_rex.write_text(
                f"""#!/usr/bin/env bash
echo "$@" >> {str(rex_log)!r}
case "$1" in
  whoami) echo '{{"window_id":"win-1"}}' ;;
  block)
    if [ "${{2:-}}" = "list" ]; then
      echo '{{"blocks":[{{"block_id":"b-side-1","window_id":"win-1","label":"faiz-dash-side"}}]}}'
    fi
    ;;
esac
""",
                encoding="utf-8",
            )
            fake_rex.chmod(0o755)
            res_close = subprocess.run([str(FAIZ_DASH_SH), "side"], env=rex_env, capture_output=True, text=True, check=True)
            self.assertIn("closed faiz-dash-side pane", res_close.stdout)
            self.assertIn("block close b-side-1", rex_log.read_text(encoding="utf-8"))

            # 4. `faiz-dash off` closes existing pane and creates dash_disabled
            rex_log.unlink()
            res_off = subprocess.run([str(FAIZ_DASH_SH), "off"], env=rex_env, capture_output=True, text=True, check=True)
            self.assertIn("auto-launch off", res_off.stdout)
            self.assertTrue((fake_home / ".agents" / "cache" / "dash_disabled").exists())
            self.assertIn("block close b-side-1", rex_log.read_text(encoding="utf-8"))

            # 5. Custom `control_center.side_ratio` in statusline_config.json overrides default 28
            rex_log.unlink()
            fake_rex.write_text(
                f"""#!/usr/bin/env bash
echo "$@" >> {str(rex_log)!r}
case "$1" in
  whoami) echo '{{"window_id":"win-1"}}' ;;
  block)
    if [ "${{2:-}}" = "list" ]; then
      echo '{{"blocks":[]}}'
    fi
    ;;
esac
""",
                encoding="utf-8",
            )
            fake_rex.chmod(0o755)
            (fake_home / ".agents" / "statusline_config.json").write_text(
                json.dumps({"control_center": {"side_ratio": 32, "dock_ratio": 26}}),
                encoding="utf-8",
            )
            res_custom = subprocess.run([str(FAIZ_DASH_SH), "side"], env=rex_env, capture_output=True, text=True, check=True)
            self.assertIn("opened faiz-dash-side pane (right, 32%)", res_custom.stdout)
            self.assertIn("--split=right --ratio 32", rex_log.read_text(encoding="utf-8"))

    def test_statusline_active_transcript_triggers_fast_45s_quota_only_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_home = root / "home"
            cache_dir = fake_home / ".agents" / "cache"
            cache_dir.mkdir(parents=True)

            now = time.time()
            u_file = cache_dir / "usage_snapshot.json"
            u_file.write_text(
                json.dumps({"pro_5h_pct": 21.36, "pro_wk_pct": 51.66, "pty_fetched_epoch": int(now - 55)}),
                encoding="utf-8",
            )
            os.utime(u_file, (now - 55, now - 55))
            p_stamp = cache_dir / ".bg_pty_ts"
            p_stamp.write_text("", encoding="utf-8")
            os.utime(p_stamp, (now - 55, now - 55))

            c_file = cache_dir / "cloud_snapshot.json"
            c_file.write_text(json.dumps({"colab_state": "DEGRADED"}), encoding="utf-8")
            os.utime(c_file, (now - 10, now - 10))
            (cache_dir / "cloud_backoff.json").write_text(
                json.dumps({"next_allowed_epoch": now + 180}),
                encoding="utf-8",
            )

            t_file = root / "transcript.jsonl"
            t_file.write_text('{"step_index":1}\n', encoding="utf-8")
            os.utime(t_file, (now - 120, now - 120))  # Older than usage_snapshot -> idle -> skip

            marker = root / "dash_args.log"
            fake_dash = root / "fake-dash"
            fake_dash.write_text(f"#!/usr/bin/env bash\necho \"$@\" >> {str(marker)!r}\n", encoding="utf-8")
            fake_dash.chmod(0o755)
            env = {
                **os.environ,
                "HOME": str(fake_home),
                "FAIZ_DASH_BIN": str(fake_dash),
                "FAIZ_TRANSCRIPT_PATH": str(t_file),
                "FAIZ_TERM_WIDTH": "91",
                "TERM": "xterm-kitty",
                "COLORTERM": "truecolor",
            }
            env.pop("FAIZ_STATUSLINE_NO_BG", None)

            subprocess.run([str(STATUSLINE_SH)], input=b"{}", env=env, capture_output=True, check=True)
            time.sleep(0.08)
            self.assertFalse(marker.exists())

            # Now simulate an active AI turn newer than usage_snapshot -> triggers 45s fast quota-only refresh
            os.utime(t_file, (now - 2, now - 2))
            subprocess.run([str(STATUSLINE_SH)], input=b"{}", env=env, capture_output=True, check=True)
            deadline = time.monotonic() + 1.5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists())
            self.assertIn("--once-json --quota-only", marker.read_text(encoding="utf-8"))

    def test_statusline_not_configured_cloud_hides_noise_and_shows_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            cache_dir = fake_home / ".agents" / "cache"
            cache_dir.mkdir(parents=True)
            (cache_dir / "usage_snapshot.json").write_text(
                json.dumps(
                    {
                        "pro_5h_pct": 80.25,
                        "pro_wk_pct": 91.5,
                        "claude_wk_pct": 88.25,
                        "argon_wk_pct": 64.0,
                        "quota_gate": "GREEN",
                        "pty_fetched_epoch": int(time.time()),
                    }
                ),
                encoding="utf-8",
            )
            (cache_dir / "cloud_snapshot.json").write_text(
                json.dumps({"colab_state": "NOT_CONFIGURED", "mailbox_tasks_count": 0}),
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "HOME": str(fake_home),
                "FAIZ_STATUSLINE_NO_BG": "1",
                "FAIZ_TRANSCRIPT_PATH": "",
                "FAIZ_TERM_WIDTH": "180",
                "TERM": "xterm-kitty",
                "COLORTERM": "truecolor",
            }
            res = subprocess.run([str(STATUSLINE_SH)], input=b"{}", env=env, capture_output=True, check=True)
            plain = re.sub(r"\x1b\[[0-9;]*m", "", res.stdout.decode("utf-8", errors="replace"))
            self.assertIn("Gemini 5h:80.25% · Wk:91.50%", plain)
            self.assertIn("Argon 64.00% · Claude 88.25%", plain)
            self.assertNotIn("Colab", plain)
            self.assertNotIn("Queue", plain)

            (fake_home / ".agents" / "statusline_config.json").write_text(
                json.dumps({"density": "expanded"}), encoding="utf-8"
            )
            res_exp = subprocess.run([str(STATUSLINE_SH)], input=b"{}", env=env, capture_output=True, check=True)
            plain_exp = re.sub(r"\x1b\[[0-9;]*m", "", res_exp.stdout.decode("utf-8", errors="replace"))
            self.assertIn("Claude 88.25%", plain_exp)

    def test_statusline_merges_claude_5h_wk_and_tracks_session_burn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_home = root / "home"
            cache_dir = fake_home / ".agents" / "cache"
            cache_dir.mkdir(parents=True)
            now = time.time()
            u_file = cache_dir / "usage_snapshot.json"
            u_file.write_text(
                json.dumps(
                    {
                        "pro_5h_pct": 0.0,
                        "pro_wk_pct": 17.11,
                        "claude_5h_pct": 70.35,
                        "claude_wk_pct": 85.17,
                        "argon_wk_pct": 9.43,
                        "quota_gate": "WARN_PRO",
                        "tokens_1h": 1200000,
                        "pty_fetched_epoch": int(now - 2),
                    }
                ),
                encoding="utf-8",
            )
            os.utime(u_file, (now - 2, now - 2))
            (cache_dir / "cloud_snapshot.json").write_text(
                json.dumps(
                    {
                        "colab_state": "ONLINE",
                        "idle_remaining_seconds": 420,
                        "mailbox_tasks_count": 0,
                        "mailbox_failed_count": 2,
                    }
                ),
                encoding="utf-8",
            )
            t_file = root / "transcript.jsonl"
            t_s = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10))
            t_file.write_text(
                json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": t_s, "input_tokens": 3500, "cache_read_tokens": 90000, "output_tokens": 900}) + "\n",
                encoding="utf-8",
            )
            os.utime(t_file, (now - 5, now - 5))

            env = {
                **os.environ,
                "HOME": str(fake_home),
                "FAIZ_STATUSLINE_NO_BG": "1",
                "FAIZ_TRANSCRIPT_PATH": str(t_file),
                "FAIZ_TERM_WIDTH": "91",
                "TERM": "xterm-kitty",
                "COLORTERM": "truecolor",
            }
            payload = b'{"context_window":{"used_percentage":17},"terminal_width":90}'
            env_90 = {k: v for k, v in env.items() if k != "FAIZ_TERM_WIDTH"}
            res_90 = subprocess.run([str(STATUSLINE_SH)], input=payload, env=env_90, capture_output=True, check=True)
            lines_90 = res_90.stdout.decode("utf-8", errors="replace").splitlines()
            self.assertEqual(len(lines_90), 4)
            plain_90 = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(lines_90)).splitlines()
            self.assertIn("─", plain_90[0])
            self.assertIn("╌", plain_90[2])
            self.assertEqual(len({len(ln) for ln in plain_90}), 1)
            self.assertLessEqual(len(plain_90[1]), 88)
            self.assertIn("Gemini 0.0%·17.1%", plain_90[1])
            self.assertIn("Arg 9.4!·Cla 70·85%", plain_90[3])
            self.assertIn("Burn 4.4K", plain_90[3])

            # At >=96 columns (A2 adaptive expansion), Cla displays full 2 decimals and stays aligned 2x4 matrix
            env_104 = {**env, "FAIZ_TERM_WIDTH": "104"}
            res_104 = subprocess.run([str(STATUSLINE_SH)], input=payload, env=env_104, capture_output=True, check=True)
            lines_104 = res_104.stdout.decode("utf-8", errors="replace").splitlines()
            self.assertEqual(len(lines_104), 4)
            plain_104_lines = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(lines_104)).splitlines()
            self.assertIn("─", plain_104_lines[0])
            self.assertIn("╌", plain_104_lines[2])
            self.assertEqual(len({len(ln) for ln in plain_104_lines}), 1)
            self.assertIn("Arg 9.43%!·Cla 70.35%·85.17%", plain_104_lines[3])

            # At >=155 columns (W1-A wide 2x4 bento matrix), full labels + merged Argon·Claude spread evenly to eff_cols
            env_180 = {**env, "FAIZ_TERM_WIDTH": "180"}
            res_180 = subprocess.run([str(STATUSLINE_SH)], input=payload, env=env_180, capture_output=True, check=True)
            lines_180 = res_180.stdout.decode("utf-8", errors="replace").splitlines()
            self.assertEqual(len(lines_180), 4)
            plain_180_lines = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(lines_180)).splitlines()
            self.assertIn("─", plain_180_lines[0])
            self.assertIn("╌", plain_180_lines[2])
            self.assertEqual(len({len(ln) for ln in plain_180_lines}), 1)
            self.assertEqual(len(plain_180_lines[1]), 178)
            self.assertIn("Gemini 5h:0.00% · Wk:17.11%", plain_180_lines[1])
            self.assertIn("Argon 9.43%! · Claude 70.35%·85.17%", plain_180_lines[3])
            self.assertIn("Queue 2 Fail", plain_180_lines[3])

            # At 60 columns (4x2 narrow matrix), merges Arg·Cla with % and renders concise Gemini without rainbow colors
            env_60 = {**env, "FAIZ_TERM_WIDTH": "60"}
            res_60 = subprocess.run([str(STATUSLINE_SH)], input=payload, env=env_60, capture_output=True, check=True)
            out_60 = res_60.stdout.decode("utf-8", errors="replace")
            lines_60 = out_60.splitlines()
            self.assertEqual(len(lines_60), 6)
            self.assertNotIn("27;52;38", out_60)
            self.assertNotIn("64;30;31", out_60)
            plain_60_lines = re.sub(r"\x1b\[[0-9;]*m", "", out_60).splitlines()
            self.assertEqual(len({len(ln) for ln in plain_60_lines}), 1)
            self.assertEqual(len(plain_60_lines[1]), 58)
            self.assertIn("Gemini 0%·17%", plain_60_lines[2])
            self.assertIn("Arg 9.4%!·Cla 70·85%", plain_60_lines[4])

            # Appending turns incrementally updates single-session Burn to 2-decimal M
            with t_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"step_index": 2, "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": t_s, "input_tokens": 1_100_000, "output_tokens": 5_600}) + "\n")
            res_inc = subprocess.run([str(STATUSLINE_SH)], input=payload, env=env_104, capture_output=True, check=True)
            plain_inc = re.sub(r"\x1b\[[0-9;]*m", "", res_inc.stdout.decode("utf-8", errors="replace"))
            self.assertIn("Burn 1.11M", plain_inc)

            # Even when usage_snapshot mtime (now-2) > transcript mtime (now-5), active <120s transcript keeps 20s cooldown
            p_stamp = cache_dir / ".bg_pty_ts"
            p_stamp.write_text("", encoding="utf-8")
            os.utime(p_stamp, (now - 25, now - 25))
            os.utime(cache_dir / "cloud_snapshot.json", (now - 5, now - 5))
            (cache_dir / "cloud_backoff.json").write_text(json.dumps({"next_allowed_epoch": now + 180}), encoding="utf-8")
            marker = root / "dash_fast.log"
            fake_dash = root / "fake-dash"
            fake_dash.write_text(f"#!/usr/bin/env bash\necho \"$@\" >> {str(marker)!r}\n", encoding="utf-8")
            fake_dash.chmod(0o755)
            env_bg = {**env_104, "FAIZ_DASH_BIN": str(fake_dash)}
            env_bg.pop("FAIZ_STATUSLINE_NO_BG", None)
            subprocess.run([str(STATUSLINE_SH)], input=payload, env=env_bg, capture_output=True, check=True)
            deadline = time.monotonic() + 1.5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists())
            self.assertIn("--once-json --quota-only", marker.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
