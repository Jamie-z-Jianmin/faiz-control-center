"""Google Drive gateway abstraction and read-only rclone adapter."""

from __future__ import annotations

import asyncio
import configparser
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Optional, Protocol, runtime_checkable

from .exec_util import resolve_binary, run_exec

DEFAULT_RCLONE_BIN = resolve_binary(
    "rclone",
    Path.home() / ".local" / "bin" / "rclone",
    "/opt/homebrew/bin/rclone",
    "/usr/local/bin/rclone",
)
DEFAULT_RCLONE_CONF = Path.home() / ".config" / "rclone" / "rclone.conf"
_LSL_LINE_RE = re.compile(
    r"^\s*(\d+)\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+(.+?)\s*$"
)


def inspect_rclone_conf(
    conf_path: Path = DEFAULT_RCLONE_CONF,
    remote: str = "gdrive",
) -> dict[str, Any]:
    """Read rclone.conf safely (without leaking tokens) to detect readonly & shared client_id."""
    result = {
        "exists": False,
        "readonly": False,
        "shared_client_id": False,
        "scope": "",
        "warnings": [],
    }
    if not conf_path.exists():
        return result

    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(conf_path, encoding="utf-8")
    except Exception:
        return result

    if not parser.has_section(remote):
        return result

    result["exists"] = True
    scope = parser.get(remote, "scope", fallback="").strip()
    client_id = parser.get(remote, "client_id", fallback="").strip()
    result["scope"] = scope
    if scope == "drive.readonly":
        result["readonly"] = True
        result["warnings"].append("rclone scope=drive.readonly (write ops disabled)")
    if not client_id:
        result["shared_client_id"] = True
        result["warnings"].append("rclone shared client_id (2026 retirement risk)")
    return result


def parse_rclone_lsl(output: str) -> list[dict[str, Any]]:
    """Parse `rclone lsl` output lines into structured file entries."""
    items: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        match = _LSL_LINE_RE.match(raw_line)
        if not match:
            continue
        size = int(match.group(1))
        ts_raw = match.group(2)
        name = match.group(3)
        ts_base = ts_raw.split(".", 1)[0]
        mtime_dt: Optional[datetime] = None
        try:
            # rclone lsl emits local wall-clock time without tz offset
            mtime_dt = datetime.strptime(ts_base, "%Y-%m-%d %H:%M:%S").astimezone(timezone.utc)
        except ValueError:
            mtime_dt = None
        items.append(
            {
                "name": name,
                "size": size,
                "modified_at": ts_base,
                "mtime_epoch": mtime_dt.timestamp() if mtime_dt else 0.0,
            }
        )
    return items


@runtime_checkable
class DriveGateway(Protocol):
    """Protocol abstracting read-only Drive operations for swappable backends."""

    async def cat_text(self, remote_path: str) -> str:
        """Read remote file text."""
        ...

    async def lsf(self, remote_path: str, dirs_only: bool = False) -> list[str]:
        """List direct child names in a remote directory."""
        ...

    async def lsl(self, remote_path: str, max_depth: int = 1) -> list[dict[str, Any]]:
        """List files with size and timestamp."""
        ...

    async def about(self) -> dict[str, Any]:
        """Return Drive storage quota statistics."""
        ...

    def capabilities(self) -> dict[str, Any]:
        """Describe read/write capabilities and active configuration warnings."""
        ...


class RcloneDriveGateway:
    """DriveGateway backed by `~/.local/bin/rclone` with 30s timeout and semaphore limit."""

    def __init__(
        self,
        *,
        remote: str = "gdrive",
        rclone_bin: str = DEFAULT_RCLONE_BIN,
        conf_path: Path = DEFAULT_RCLONE_CONF,
        timeout: float = 30.0,
        semaphore: Optional[asyncio.Semaphore] = None,
    ) -> None:
        self.remote = remote
        self.rclone_bin = rclone_bin
        self.conf_path = conf_path
        self.timeout = timeout
        self.semaphore = semaphore
        self._conf_info = inspect_rclone_conf(conf_path, remote=remote)

    def _remote_spec(self, remote_path: str) -> str:
        cleaned = remote_path.lstrip("/")
        return f"{self.remote}:{cleaned}"

    def capabilities(self) -> dict[str, Any]:
        self._conf_info = inspect_rclone_conf(self.conf_path, remote=self.remote)
        has_bin = Path(resolve_binary(self.rclone_bin, self.rclone_bin)).exists()
        return {
            "configured": bool(self._conf_info["exists"] and has_bin),
            "read": True,
            "write": not self._conf_info["readonly"] and self._conf_info["exists"],
            "readonly": self._conf_info["readonly"],
            "shared_client_id": self._conf_info["shared_client_id"],
            "warnings": list(self._conf_info["warnings"]),
        }

    def _rclone_cmd(self, *args: str) -> list[str]:
        return [
            self.rclone_bin,
            "--retries",
            "1",
            "--low-level-retries",
            "2",
            *args,
        ]

    async def cat_text(self, remote_path: str) -> str:
        res = await run_exec(
            self._rclone_cmd("cat", self._remote_spec(remote_path)),
            semaphore=self.semaphore,
            timeout=self.timeout,
        )
        if res.timed_out or res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or f"rclone cat failed ({res.returncode})")
        return res.stdout

    async def lsf(self, remote_path: str, dirs_only: bool = False) -> list[str]:
        argv = ["lsf"]
        if dirs_only:
            argv.append("--dirs-only")
        argv.append(self._remote_spec(remote_path))
        res = await run_exec(self._rclone_cmd(*argv), semaphore=self.semaphore, timeout=self.timeout)
        if res.timed_out or res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or f"rclone lsf failed ({res.returncode})")
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]

    async def lsl(self, remote_path: str, max_depth: int = 1) -> list[dict[str, Any]]:
        res = await run_exec(
            self._rclone_cmd("lsl", "--max-depth", str(max_depth), self._remote_spec(remote_path)),
            semaphore=self.semaphore,
            timeout=self.timeout,
        )
        if res.timed_out or res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or f"rclone lsl failed ({res.returncode})")
        return parse_rclone_lsl(res.stdout)

    async def about(self) -> dict[str, Any]:
        res = await run_exec(
            self._rclone_cmd("about", f"{self.remote}:", "--json"),
            semaphore=self.semaphore,
            timeout=self.timeout,
        )
        if res.timed_out or res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or f"rclone about failed ({res.returncode})")
        return json.loads(res.stdout)
