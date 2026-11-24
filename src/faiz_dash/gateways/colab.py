"""Read-only google-colab-cli (`colab --auth=adc`) control-plane gateway."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import shutil
from typing import Any, Optional

from ..models import redact_secrets
from .exec_util import resolve_binary, run_exec

DEFAULT_COLAB_BIN = resolve_binary(
    "colab",
    Path.home() / ".local" / "bin" / "colab",
    "/opt/homebrew/bin/colab",
    "/usr/local/bin/colab",
)

_SESSION_LINE_RE = re.compile(r"^\s*\[([^\]]+)\]\s+(\S+)(?:\s*\|\s*(.*))?$")
_BALANCE_RE = re.compile(r"Current balance:\s*([0-9]+(?:\.[0-9]+)?)\s*compute units", re.IGNORECASE)
_RATE_RE = re.compile(r"Usage rate:\s*(.+)", re.IGNORECASE)
_ACTIVE_RE = re.compile(r"Active assignments:\s*(\d+)", re.IGNORECASE)


def parse_colab_sessions(output: str) -> tuple[dict[str, Any], ...]:
    """Parse plain-text output from `colab --auth=adc sessions` or `status`."""
    sessions: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or "no active sessions found" in line.lower():
            continue
        match = _SESSION_LINE_RE.match(line)
        if not match:
            continue
        name, endpoint, rest = match.group(1).strip(), match.group(2).strip(), (match.group(3) or "")
        if name.lower() == "colab":
            continue
        kv: dict[str, str] = {}
        for part in rest.split("|"):
            seg = part.strip()
            if ":" in seg:
                k, v = seg.split(":", 1)
                kv[k.strip().lower()] = v.strip()
        entry: dict[str, Any] = {
            "name": name,
            "endpoint": endpoint,
            "hardware": kv.get("hardware", ""),
            "shape": kv.get("shape", ""),
            "variant": kv.get("variant", ""),
        }
        if "status" in kv:
            entry["status"] = kv["status"]
        sessions.append(entry)
    return tuple(sessions)


def parse_colab_usage(output: str) -> dict[str, Any]:
    """Parse plain-text output from `colab --auth=adc usage`."""
    info: dict[str, Any] = {}
    m_bal = _BALANCE_RE.search(output)
    if m_bal:
        info["balance_cu"] = float(m_bal.group(1))
    m_rate = _RATE_RE.search(output)
    if m_rate:
        info["usage_rate"] = m_rate.group(1).strip()
    m_act = _ACTIVE_RE.search(output)
    if m_act:
        info["active_assignments"] = int(m_act.group(1))
    return info


class ColabCliGateway:
    """Read-only Colab control-plane adapter backed by `colab --auth=adc`."""

    def __init__(
        self,
        *,
        colab_bin: Optional[str] = None,
        timeout: float = 10.0,
        semaphore: Optional[asyncio.Semaphore] = None,
        config_dir: Optional[Path] = None,
        adc_path: Optional[Path] = None,
    ) -> None:
        self.colab_bin = colab_bin or resolve_binary(
            "colab",
            Path.home() / ".local" / "bin" / "colab",
            "/opt/homebrew/bin/colab",
            "/usr/local/bin/colab",
        )
        self.timeout = timeout
        self.semaphore = semaphore
        self.config_dir = config_dir or (Path.home() / ".config" / "colab-cli")
        self.adc_path = adc_path or (
            Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        )

    def is_configured(self) -> bool:
        """Return True when `colab` binary and ADC or colab-cli credentials exist."""
        bin_path = Path(self.colab_bin).expanduser()
        has_bin = (bin_path.is_file() and os.access(bin_path, os.X_OK)) or bool(
            shutil.which(self.colab_bin)
        )
        if not has_bin:
            return False
        return self.adc_path.exists() or self.config_dir.exists()

    async def list_sessions(self) -> tuple[dict[str, Any], ...]:
        """Run `colab --auth=adc sessions` (read-only) and return active session dicts."""
        res = await run_exec(
            [self.colab_bin, "--auth=adc", "sessions"],
            semaphore=self.semaphore,
            timeout=self.timeout,
        )
        if res.timed_out or res.returncode != 0:
            err_msg = (res.stderr or res.stdout).strip() or f"colab sessions failed ({res.returncode})"
            raise RuntimeError(redact_secrets(err_msg))
        return parse_colab_sessions(redact_secrets(res.stdout))

    async def get_usage(self) -> dict[str, Any]:
        """Run `colab --auth=adc usage` (read-only) and return compute-unit balance info."""
        res = await run_exec(
            [self.colab_bin, "--auth=adc", "usage"],
            semaphore=self.semaphore,
            timeout=self.timeout,
        )
        if res.timed_out or res.returncode != 0:
            err_msg = (res.stderr or res.stdout).strip() or f"colab usage failed ({res.returncode})"
            raise RuntimeError(redact_secrets(err_msg))
        return parse_colab_usage(redact_secrets(res.stdout))
