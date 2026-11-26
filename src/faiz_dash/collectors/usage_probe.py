"""Usage PTY probe, channel detection, and incremental transcript burn collector."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import sqlite3
import struct
import subprocess
import termios
import time
from typing import Any, Optional
import unicodedata

from ..gateways.exec_util import resolve_binary, run_exec
from ..models import (
    ModelBucketQuota,
    ProcessChannelInfo,
    QuotaSnapshot,
    TokenBurnStats,
)
from ..store import DEFAULT_CACHE_DIR, atomic_write_json

DEFAULT_AGY_BIN = resolve_binary("agy", "/usr/local/bin/agy", "/opt/homebrew/bin/agy")
DEFAULT_DB_PATH = Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db"
DEFAULT_BRAIN_DIR = Path.home() / ".gemini" / "antigravity-cli" / "brain"
DEFAULT_CLI_LOG = Path.home() / ".gemini" / "antigravity-cli" / "cli.log"

_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_ESC_RE = re.compile(r"\x1b[@-Z\\-_]")
_ACCOUNT_RE = re.compile(r"Account:\s*([A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+|[^\s\r\n%]+)")
_MODELS_RE = re.compile(r"Models within this group:\s*([^\r\n]+)")
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)%")
_REFRESH_RE = re.compile(r"(Refreshes in [^\r\n]+|Quota available)")
_AUTH_LOG_RE = re.compile(
    r"applyAuthResult:\s*email=[^,]+,\s*authMethod=([a-zA-Z0-9_-]+),\s*quotaProject=([^\s,]*)"
)

_SECTION_HEADERS = (
    "GEMINI MODELS",
    "CLAUDE AND GPT MODELS",
)


def strip_ansi(raw_text: str) -> str:
    """Remove ANSI/VT escape sequences and carriage returns from PTY output."""
    text = _ANSI_CSI_RE.sub("", raw_text)
    text = _ANSI_OSC_RE.sub("", text)
    text = _ANSI_ESC_RE.sub("", text)
    return text.replace("\r", "")


def render_pty_screen(raw_text: str, *, rows: int = 60, cols: int = 140) -> str:
    """Replay VT/CSI cursor positioning and erases onto a rows x cols screen grid."""
    grid = [[" "] * cols for _ in range(rows)]
    r = 0
    c = 0
    wrap_pending = False
    saved = (0, 0)
    i = 0
    n = len(raw_text)

    def _scroll_if_needed() -> None:
        nonlocal r
        while r >= rows:
            grid.pop(0)
            grid.append([" "] * cols)
            r -= 1

    while i < n:
        ch = raw_text[i]
        if ch == "\x1b":
            if i + 1 < n and raw_text[i + 1] == "[":
                j = i + 2
                while j < n and not ("@" <= raw_text[j] <= "~"):
                    j += 1
                if j < n:
                    params = raw_text[i + 2 : j]
                    final = raw_text[j]
                    i = j + 1
                    priv = params.startswith("?")
                    p_str = params[1:] if priv else params
                    nums = [int(x) if x.isdigit() else 0 for x in p_str.split(";")] if p_str else []
                    if priv:
                        if final in ("h", "l") and any(x in (47, 1047, 1049) for x in nums):
                            grid = [[" "] * cols for _ in range(rows)]
                            r, c, wrap_pending = 0, 0, False
                        continue
                    if final in ("H", "f"):
                        rr = (nums[0] if len(nums) >= 1 and nums[0] > 0 else 1) - 1
                        cc = (nums[1] if len(nums) >= 2 and nums[1] > 0 else 1) - 1
                        r = max(0, min(rows - 1, rr))
                        c = max(0, min(cols - 1, cc))
                        wrap_pending = False
                    elif final == "G":
                        cc = (nums[0] if nums and nums[0] > 0 else 1) - 1
                        c = max(0, min(cols - 1, cc))
                        wrap_pending = False
                    elif final == "A":
                        r = max(0, r - (nums[0] if nums and nums[0] > 0 else 1))
                        wrap_pending = False
                    elif final == "B":
                        r = min(rows - 1, r + (nums[0] if nums and nums[0] > 0 else 1))
                        wrap_pending = False
                    elif final == "C":
                        c = min(cols - 1, c + (nums[0] if nums and nums[0] > 0 else 1))
                        wrap_pending = False
                    elif final == "D":
                        c = max(0, c - (nums[0] if nums and nums[0] > 0 else 1))
                        wrap_pending = False
                    elif final == "E":
                        r = min(rows - 1, r + (nums[0] if nums and nums[0] > 0 else 1))
                        c = 0
                        wrap_pending = False
                    elif final == "F":
                        r = max(0, r - (nums[0] if nums and nums[0] > 0 else 1))
                        c = 0
                        wrap_pending = False
                    elif final == "J":
                        mode = nums[0] if nums else 0
                        if mode in (2, 3):
                            grid = [[" "] * cols for _ in range(rows)]
                        elif mode == 0:
                            for cc in range(c, cols):
                                grid[r][cc] = " "
                            for rr in range(r + 1, rows):
                                grid[rr] = [" "] * cols
                        elif mode == 1:
                            for rr in range(0, r):
                                grid[rr] = [" "] * cols
                            for cc in range(0, c + 1):
                                grid[r][cc] = " "
                        wrap_pending = False
                    elif final == "K":
                        mode = nums[0] if nums else 0
                        if mode == 0:
                            for cc in range(c, cols):
                                grid[r][cc] = " "
                        elif mode == 1:
                            for cc in range(0, c + 1):
                                grid[r][cc] = " "
                        elif mode == 2:
                            grid[r] = [" "] * cols
                        wrap_pending = False
                    elif final == "s":
                        saved = (r, c)
                    elif final == "u":
                        r, c = saved
                        wrap_pending = False
                    continue
            elif i + 1 < n and raw_text[i + 1] == "]":
                j = i + 2
                while j < n:
                    if raw_text[j] == "\x07":
                        j += 1
                        break
                    if raw_text[j] == "\x1b" and j + 1 < n and raw_text[j + 1] == "\\":
                        j += 2
                        break
                    j += 1
                i = j
                continue
            else:
                if i + 1 < n and raw_text[i + 1] == "7":
                    saved = (r, c)
                elif i + 1 < n and raw_text[i + 1] == "8":
                    r, c = saved
                    wrap_pending = False
                i += 2
                continue

        if ch == "\r":
            c = 0
            wrap_pending = False
            i += 1
            continue
        if ch == "\n":
            r += 1
            c = 0
            wrap_pending = False
            _scroll_if_needed()
            i += 1
            continue
        if ch == "\b":
            c = max(0, c - 1)
            wrap_pending = False
            i += 1
            continue
        if ch == "\t":
            c = min(cols - 1, (c // 8 + 1) * 8)
            wrap_pending = False
            i += 1
            continue
        if ord(ch) < 32 or unicodedata.combining(ch):
            i += 1
            continue

        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if wrap_pending or c + w > cols:
            r += 1
            c = 0
            wrap_pending = False
            _scroll_if_needed()
        grid[r][c] = ch
        if w == 2 and c + 1 < cols:
            grid[r][c + 1] = ""
        c += w
        if c >= cols:
            c = cols - 1
            wrap_pending = True
        i += 1

    return "\n".join("".join(row).rstrip() for row in grid).rstrip()


def _parse_limit_block(section_text: str, heading_pattern: str) -> tuple[Optional[float], str]:
    match = re.search(heading_pattern, section_text)
    if not match:
        return None, ""
    tail = section_text[match.end() : match.end() + 260]
    pct_match = _PCT_RE.search(tail)
    pct = float(pct_match.group(1)) if pct_match else None
    ref_match = _REFRESH_RE.search(tail)
    refresh = ref_match.group(1).strip() if ref_match else ""
    return pct, refresh


def parse_usage_text(raw_text: str) -> dict[str, Any]:
    """Parse captured Antigravity `/usage` TUI text into structured quota buckets."""
    clean = render_pty_screen(raw_text)
    acc_match = _ACCOUNT_RE.search(clean)
    account = acc_match.group(1).strip() if acc_match else ""

    positions: list[tuple[int, str]] = []
    for header in _SECTION_HEADERS:
        # Search from the end so live PTY repaints supersede earlier cached frames.
        idx = clean.rfind(header)
        if idx != -1:
            positions.append((idx, header))
    positions.sort(key=lambda item: item[0])

    buckets: list[dict[str, Any]] = []
    for i, (start_idx, header) in enumerate(positions):
        end_idx = positions[i + 1][0] if i + 1 < len(positions) else len(clean)
        chunk = clean[start_idx:end_idx]
        stop_marker = chunk.find("Within each group")
        if stop_marker != -1:
            chunk = chunk[:stop_marker]

        models: list[str] = []
        m_match = _MODELS_RE.search(chunk)
        if m_match:
            models = [m.strip() for m in m_match.group(1).split(",") if m.strip()]

        weekly_pct, weekly_refresh = _parse_limit_block(chunk, r"Weekly Limit Remaining")
        five_pct, five_refresh = _parse_limit_block(chunk, r"Five Hour Limit Remaining")

        buckets.append(
            {
                "group_name": header,
                "models": models,
                "weekly_pct": weekly_pct,
                "weekly_refresh": weekly_refresh,
                "five_hour_pct": five_pct,
                "five_hour_refresh": five_refresh,
            }
        )

    return {"account": account, "buckets": buckets}


def _is_usage_frame1_complete(buf: bytes, *, elapsed: float = 0.0) -> bool:
    """Detect whether Frame 1 of `/usage` output has finished rendering in the PTY buffer."""
    footer_idx = buf.rfind(b"Within each group")
    if footer_idx != -1:
        prefix = buf[:footer_idx]
        if b"CLAUDE AND GPT MODELS" in prefix or b"GEMINI MODELS" in prefix:
            last_hdr_end = -1
            for hdr in (b"GEMINI MODELS", b"CLAUDE AND GPT MODELS"):
                idx = prefix.rfind(hdr)
                if idx != -1:
                    end_pos = idx + len(hdr)
                    if end_pos > last_hdr_end:
                        last_hdr_end = end_pos
            if last_hdr_end != -1:
                tail = prefix[last_hdr_end:]
                if b"%" in tail and (b"Refreshes in" in tail or b"Quota available" in tail):
                    return True

    if elapsed >= 4.0 and b"CLAUDE AND GPT MODELS" in buf:
        claude_tail = buf[buf.rfind(b"CLAUDE AND GPT MODELS") :]
        five_hdr = b"Five Hour Limit Remaining"
        five_idx = claude_tail.rfind(five_hdr)
        if five_idx != -1:
            after_five = claude_tail[five_idx + len(five_hdr) :]
            if b"%" in after_five and (b"Refreshes in" in after_five or b"Quota available" in after_five):
                return True

    return False


def _read_fresh_usage_cache(cache_path: Path, cooldown_sec: float) -> Optional[dict[str, Any]]:
    if not cache_path.exists():
        return None
    try:
        st_mt = cache_path.stat().st_mtime
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("buckets"):
            return None
        pty_ep = float(data.get("pty_fetched_epoch") or st_mt)
        age = time.time() - pty_ep
        if age > cooldown_sec:
            return None
        data["pty_fetched_epoch"] = int(pty_ep)
        data["from_cache"] = True
        return data
    except Exception:
        return None


def run_pty_usage_probe(
    *,
    cache_path: Optional[Path] = None,
    lock_path: Optional[Path] = None,
    agy_bin: str = DEFAULT_AGY_BIN,
    cooldown_sec: float = 35.0,
    timeout_sec: float = 14.0,
    force: bool = False,
) -> dict[str, Any]:
    """Run `/usage` inside an isolated 60x140 PTY with flock and process-group reaping."""
    cache_file = cache_path or (DEFAULT_CACHE_DIR / "usage_snapshot.json")
    lock_file = lock_path or (DEFAULT_CACHE_DIR / "usage_probe.lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    if not force:
        cached = _read_fresh_usage_cache(cache_file, cooldown_sec)
        if cached is not None:
            return cached

    lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            cached = _read_fresh_usage_cache(cache_file, max(cooldown_sec, 3600.0))
            if cached is not None:
                cached["lock_busy"] = True
                return cached
            return {"account": "", "buckets": [], "error": "usage probe lock busy", "lock_busy": True}

        if not force:
            cached = _read_fresh_usage_cache(cache_file, cooldown_sec)
            if cached is not None:
                return cached

        master_fd, slave_fd = pty.openpty()
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 140, 0, 0))

        env = os.environ.copy()
        env["FAIZ_NO_DASH"] = "1"
        env["FAIZ_STATUSLINE_SILENT"] = "1"
        env.pop("AGY_ADC_AUTH", None)
        env["TERM"] = "xterm-256color"

        proc: Optional[subprocess.Popen[bytes]] = None
        buf = bytearray()
        sent = False
        start = time.monotonic()
        frame1_seen_at: Optional[float] = None
        last_chunk_at = start
        post_frame1_bytes = 0
        saw_post_frame1_repaint = False

        try:
            proc = subprocess.Popen(
                [agy_bin],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                preexec_fn=os.setsid,
                close_fds=True,
            )
            os.close(slave_fd)
            slave_fd = -1

            while time.monotonic() - start < timeout_sec:
                readable, _, _ = select.select([master_fd], [], [], 0.15)
                chunk = b""
                if readable:
                    try:
                        chunk = os.read(master_fd, 8192)
                        if not chunk:
                            break
                        buf.extend(chunk)
                    except OSError:
                        break

                now = time.monotonic()
                elapsed = now - start
                if not sent and (b"Google AI Pro" in buf or b"for shortcuts" in buf) and elapsed > 1.2:
                    time.sleep(0.25)
                    os.write(master_fd, b"/usage")
                    time.sleep(0.15)
                    os.write(master_fd, b"\r")
                    sent = True
                elif not sent and elapsed > 4.5:
                    os.write(master_fd, b"/usage")
                    time.sleep(0.15)
                    os.write(master_fd, b"\r")
                    sent = True

                if frame1_seen_at is None:
                    if sent and _is_usage_frame1_complete(bytes(buf), elapsed=elapsed):
                        frame1_seen_at = now
                        last_chunk_at = now
                else:
                    if chunk:
                        since_f1 = now - frame1_seen_at
                        gap = now - last_chunk_at
                        last_chunk_at = now
                        if since_f1 >= 0.45:
                            post_frame1_bytes += len(chunk)
                            if (
                                b"%" in chunk
                                or b"Refreshes in" in chunk
                                or b"Quota available" in chunk
                                or (gap >= 0.5 and post_frame1_bytes >= 80)
                            ):
                                saw_post_frame1_repaint = True
                    settle_cap = min(start + timeout_sec, frame1_seen_at + 4.8)
                    if now >= settle_cap:
                        break
                    if saw_post_frame1_repaint and (now - last_chunk_at) >= 0.85:
                        break
        finally:
            if proc is not None:
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGTERM)
                    deadline = time.monotonic() + 0.4
                    while proc.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.05)
                    if proc.poll() is None:
                        os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except Exception:
                    pass
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
            if slave_fd != -1:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass
            try:
                os.close(master_fd)
            except OSError:
                pass

        parsed = parse_usage_text(buf.decode("utf-8", errors="replace"))
        if not parsed.get("buckets"):
            parsed["error"] = "PTY /usage timed out or returned no quota buckets"
        else:
            parsed["from_cache"] = False
            parsed["pty_fetched_epoch"] = int(time.time())
        return parsed
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


def parse_ps_eww_channels(ps_output: str) -> tuple[int, int]:
    """Count running Pro vs GCP (`AGY_ADC_AUTH=1`) agy CLI processes from `ps eww`."""
    pro_count = 0
    gcp_count = 0
    for line in ps_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 2)
        if len(parts) < 2:
            continue
        exe = parts[1]
        if Path(exe).name != "agy":
            continue
        if "FAIZ_NO_DASH=1" in stripped:
            # Skip ephemeral /usage PTY probe child if caught mid-flight
            continue
        if "AGY_ADC_AUTH=1" in stripped:
            gcp_count += 1
        else:
            pro_count += 1
    return pro_count, gcp_count


async def detect_process_channels(
    *,
    cli_log_path: Path = DEFAULT_CLI_LOG,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> ProcessChannelInfo:
    """Identify active `faiz`(Pro) vs `faiz-gcp` processes and last cli.log auth result."""
    res = await run_exec(["ps", "eww", "-axo", "pid,command"], semaphore=semaphore, timeout=3.0)
    pro_count, gcp_count = parse_ps_eww_channels(res.stdout) if res.returncode == 0 else (0, 0)

    auth_method = ""
    quota_project = ""
    if cli_log_path.exists():
        try:
            size = cli_log_path.stat().st_size
            read_from = max(0, size - 32_768)
            with cli_log_path.open("rb") as fh:
                fh.seek(read_from)
                tail = fh.read().decode("utf-8", errors="replace")
            matches = _AUTH_LOG_RE.findall(tail)
            if matches:
                auth_method, quota_project = matches[-1]
        except OSError:
            pass

    return ProcessChannelInfo(
        pro_count=pro_count,
        gcp_count=gcp_count,
        quota_project=quota_project.strip(),
        active_auth_method=auth_method.strip(),
    )


def _parse_iso_timestamp(ts_str: str) -> Optional[float]:
    if not ts_str:
        return None
    try:
        normalized = ts_str.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def collect_transcript_burn(
    *,
    db_path: Path = DEFAULT_DB_PATH,
    brain_dir: Path = DEFAULT_BRAIN_DIR,
    offsets_path: Optional[Path] = None,
    now_epoch: Optional[float] = None,
) -> TokenBurnStats:
    """Incrementally tail Top-10 active transcripts by byte_offset for 1h/24h token burn."""
    now_ts = now_epoch if now_epoch is not None else time.time()
    cutoff_24h = now_ts - 86400.0
    cutoff_1h = now_ts - 3600.0
    offsets_file = offsets_path or (DEFAULT_CACHE_DIR / "dash_offsets.json")

    if not db_path.exists():
        return TokenBurnStats()

    offsets_state: dict[str, Any] = {}
    if offsets_file.exists():
        try:
            loaded = json.loads(offsets_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                offsets_state = loaded
        except Exception:
            offsets_state = {}

    rows: list[tuple[str, Any]] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            conn.execute("PRAGMA query_only = ON")
            cur = conn.execute(
                """
                SELECT conversation_id, not_fully_idle
                FROM conversation_summaries
                ORDER BY last_modified_time DESC
                LIMIT 10
                """
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return TokenBurnStats()

    active_sessions = sum(1 for _, idle_flag in rows if bool(idle_flag))
    changed = False

    for conv_id, _ in rows:
        t_path = brain_dir / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
        if not t_path.exists():
            continue
        try:
            st = t_path.stat()
        except OSError:
            continue

        entry = offsets_state.get(conv_id)
        if not isinstance(entry, dict):
            entry = {"byte_offset": 0, "mtime": 0.0, "records": []}
            offsets_state[conv_id] = entry

        prev_offset = int(entry.get("byte_offset", 0))
        records: list[dict[str, Any]] = list(entry.get("records", []))

        if st.st_size < prev_offset:
            prev_offset = 0
            records = []

        if st.st_size > prev_offset:
            try:
                with t_path.open("rb") as fh:
                    fh.seek(prev_offset)
                    chunk = fh.read()
                new_offset = prev_offset + len(chunk)
                last_nl = chunk.rfind(b"\n")
                if last_nl != -1:
                    complete_bytes = chunk[: last_nl + 1]
                    new_offset = prev_offset + len(complete_bytes)
                    for raw_line in complete_bytes.decode("utf-8", errors="replace").splitlines():
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        in_tok = int(obj.get("input_tokens") or 0)
                        cache_tok = int(obj.get("cache_read_tokens") or 0)
                        out_tok = int(obj.get("output_tokens") or 0)
                        if in_tok == 0 and cache_tok == 0 and out_tok == 0:
                            continue
                        ts_val = _parse_iso_timestamp(str(obj.get("created_at") or "")) or st.st_mtime
                        records.append(
                            {
                                "ts": ts_val,
                                "model": str(obj.get("model") or "unknown"),
                                "in": in_tok,
                                "cache": cache_tok,
                                "out": out_tok,
                            }
                        )
                    entry["byte_offset"] = new_offset
                    entry["mtime"] = st.st_mtime
                    changed = True
            except OSError:
                pass

        pruned = [r for r in records if float(r.get("ts", 0)) >= cutoff_24h]
        if len(pruned) != len(records):
            changed = True
        entry["records"] = pruned[-2000:]

    if changed:
        # Keep offset map bounded to recent 50 conversations
        if len(offsets_state) > 50:
            sorted_keys = sorted(
                offsets_state.keys(),
                key=lambda k: float(offsets_state[k].get("mtime", 0.0)),
                reverse=True,
            )[:50]
            offsets_state = {k: offsets_state[k] for k in sorted_keys}
        atomic_write_json(offsets_file, offsets_state)

    tokens_1h = 0
    tokens_24h = 0
    total_input_24h = 0
    total_cache_24h = 0
    by_model: dict[str, int] = {}

    for entry in offsets_state.values():
        if not isinstance(entry, dict):
            continue
        for rec in entry.get("records", []):
            ts_val = float(rec.get("ts", 0.0))
            if ts_val < cutoff_24h:
                continue
            in_tok = int(rec.get("in", 0))
            cache_tok = int(rec.get("cache", 0))
            out_tok = int(rec.get("out", 0))
            total_tok = in_tok + out_tok
            tokens_24h += total_tok
            total_input_24h += in_tok
            total_cache_24h += cache_tok
            model = str(rec.get("model") or "unknown")
            by_model[model] = by_model.get(model, 0) + total_tok
            if ts_val >= cutoff_1h:
                tokens_1h += total_tok

    total_prompt_24h = total_input_24h + total_cache_24h
    hit_rate = (total_cache_24h / total_prompt_24h) if total_prompt_24h > 0 else 0.0
    hit_rate = max(0.0, min(1.0, hit_rate))

    return TokenBurnStats(
        tokens_1h=tokens_1h,
        tokens_24h=tokens_24h,
        cache_hit_rate=round(hit_rate, 4),
        active_sessions=active_sessions,
        by_model_24h=dict(sorted(by_model.items(), key=lambda kv: kv[1], reverse=True)[:6]),
    )


class UsageProbe:
    """Coordinator combining PTY /usage probe, channel ps/log detection, and transcript burn."""

    def __init__(
        self,
        *,
        cache_dir: Optional[Path] = None,
        agy_bin: str = DEFAULT_AGY_BIN,
        db_path: Path = DEFAULT_DB_PATH,
        brain_dir: Path = DEFAULT_BRAIN_DIR,
        cli_log_path: Path = DEFAULT_CLI_LOG,
        semaphore: Optional[asyncio.Semaphore] = None,
        pty_cooldown_sec: float = 15.0,
    ) -> None:
        self.cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self.agy_bin = agy_bin
        self.db_path = db_path
        self.brain_dir = brain_dir
        self.cli_log_path = cli_log_path
        self.semaphore = semaphore
        self.pty_cooldown_sec = pty_cooldown_sec
        self._last_buckets: tuple[ModelBucketQuota, ...] = ()
        self._last_account: str = ""

    async def collect(self, *, probe_pty: bool = True, force_pty: bool = False) -> QuotaSnapshot:
        now_iso = datetime.now(timezone.utc).isoformat()
        channels_task = detect_process_channels(
            cli_log_path=self.cli_log_path,
            semaphore=self.semaphore,
        )
        burn_task = asyncio.to_thread(
            collect_transcript_burn,
            db_path=self.db_path,
            brain_dir=self.brain_dir,
            offsets_path=self.cache_dir / "dash_offsets.json",
        )

        pty_data: dict[str, Any] = {}
        error: Optional[str] = None
        source = "local"
        stale = False

        if probe_pty:
            try:
                pty_data = await asyncio.to_thread(
                    run_pty_usage_probe,
                    cache_path=self.cache_dir / "usage_snapshot.json",
                    lock_path=self.cache_dir / "usage_probe.lock",
                    agy_bin=self.agy_bin,
                    cooldown_sec=self.pty_cooldown_sec,
                    timeout_sec=14.0,
                    force=force_pty,
                )
                source = "cache" if pty_data.get("from_cache") else "pty"
                if pty_data.get("error"):
                    error = str(pty_data["error"])
            except Exception as exc:
                error = str(exc)
        else:
            cached = _read_fresh_usage_cache(self.cache_dir / "usage_snapshot.json", 86400.0)
            if cached:
                pty_data = cached
                source = "cache"

        channels, burn = await asyncio.gather(channels_task, burn_task)

        raw_buckets = pty_data.get("buckets") or []
        if raw_buckets:
            buckets = tuple(
                ModelBucketQuota(
                    group_name=str(b.get("group_name", "")),
                    models=tuple(b.get("models") or ()),
                    weekly_pct=b.get("weekly_pct"),
                    weekly_refresh=str(b.get("weekly_refresh", "")),
                    five_hour_pct=b.get("five_hour_pct"),
                    five_hour_refresh=str(b.get("five_hour_refresh", "")),
                )
                for b in raw_buckets
            )
            account = str(pty_data.get("account") or self._last_account)
            self._last_buckets = buckets
            self._last_account = account
        else:
            buckets = self._last_buckets
            account = self._last_account
            if buckets:
                stale = True

        pty_epoch = int(pty_data.get("pty_fetched_epoch") or (time.time() if source == "pty" and raw_buckets else 0))
        effective_fetched_at = str(pty_data.get("fetched_at") or now_iso) if source == "cache" else now_iso

        return QuotaSnapshot(
            fetched_at=effective_fetched_at,
            account=account,
            buckets=buckets,
            channels=channels,
            burn=burn,
            source=source,
            pty_fetched_epoch=pty_epoch,
            error=error,
            stale=stale,
        )
