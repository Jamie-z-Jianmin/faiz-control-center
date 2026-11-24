"""Google Drive gateway abstraction, gdrive CLI adapter, and read-only rclone adapter."""

from __future__ import annotations

import asyncio
import configparser
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Optional, Protocol, runtime_checkable

from ..models import redact_secrets
from .exec_util import resolve_binary, run_exec

DEFAULT_RCLONE_BIN = resolve_binary(
    "rclone",
    Path.home() / ".local" / "bin" / "rclone",
    "/opt/homebrew/bin/rclone",
    "/usr/local/bin/rclone",
)
DEFAULT_RCLONE_CONF = Path.home() / ".config" / "rclone" / "rclone.conf"
_FOLDER_MIME = "application/vnd.google-apps.folder"
_LSL_LINE_RE = re.compile(
    r"^\s*(\d+)\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+(.+?)\s*$"
)
_QUOTA_LINE_RE = re.compile(
    r"(?im)^\s*(Used|Limit|Total|Free)\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*(B|KB|KiB|MB|MiB|GB|GiB|TB|TiB)\b"
)
_UNIT_MULT = {
    "B": 1,
    "KB": 1024,
    "KIB": 1024,
    "MB": 1024**2,
    "MIB": 1024**2,
    "GB": 1024**3,
    "GIB": 1024**3,
    "TB": 1024**4,
    "TIB": 1024**4,
}


def _resolve_real_gdrive_bin() -> str:
    """Resolve `gdrive` CLI, avoiding ephemeral `agy-plugin-cli-*` shims in PATH."""
    found = shutil.which("gdrive")
    if found and "agy-plugin-cli-" not in found:
        return found
    for cand in (
        Path.home() / ".gemini" / "antigravity-cli" / "bin" / "gdrive",
        Path("/opt/homebrew/bin/gdrive"),
        Path("/usr/local/bin/gdrive"),
    ):
        if os.access(cand, os.X_OK):
            return str(cand)
    return resolve_binary(
        "gdrive",
        Path.home() / ".gemini" / "antigravity-cli" / "bin" / "gdrive",
        "/opt/homebrew/bin/gdrive",
        "/usr/local/bin/gdrive",
    )


def _sanitized_gdrive_env(gdrive_bin: Optional[str] = None) -> dict[str, str]:
    """Strip `agy-plugin-cli-*` shims from PATH and ensure real `gdrive` dir is on PATH."""
    env = {k: v for k, v in os.environ.items() if k != "ANTIGRAVITY_PLUGIN_SHIM_DIR"}
    segs = [seg for seg in env.get("PATH", "").split(os.pathsep) if seg and "agy-plugin-cli-" not in seg]
    if gdrive_bin:
        gdir = str(Path(gdrive_bin).expanduser().parent)
        if gdir and gdir not in segs:
            segs.insert(0, gdir)
    env["PATH"] = os.pathsep.join(segs)
    return env


def _to_int_or_none(val: Any) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def parse_gdrive_quota(output: str) -> dict[str, int]:
    """Parse `gdrive readonly quota [--json]` JSON or text output into `{used, total, free}`."""
    raw = (output or "").strip()
    if not raw:
        return {}

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            sq = data.get("storageQuota") if isinstance(data.get("storageQuota"), dict) else data
            used = _to_int_or_none(
                sq.get("used") if sq.get("used") is not None else sq.get("usage", sq.get("used_bytes"))
            )
            total = _to_int_or_none(
                sq.get("total")
                if sq.get("total") is not None
                else sq.get("limit", sq.get("total_bytes"))
            )
            free = _to_int_or_none(
                sq.get("free") if sq.get("free") is not None else sq.get("free_bytes")
            )
            if free is None and total is not None and used is not None:
                free = max(0, total - used)
            out: dict[str, int] = {}
            if used is not None:
                out["used"] = used
            if total is not None:
                out["total"] = total
            if free is not None:
                out["free"] = free
            return out
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    parsed: dict[str, int] = {}
    for m in _QUOTA_LINE_RE.finditer(raw):
        label = m.group(1).lower()
        num = float(m.group(2))
        mult = _UNIT_MULT.get(m.group(3).upper(), 1)
        val_bytes = int(round(num * mult))
        if label == "used":
            parsed["used"] = val_bytes
        elif label in {"limit", "total"}:
            parsed["total"] = val_bytes
        elif label == "free":
            parsed["free"] = val_bytes
    if "free" not in parsed and "total" in parsed and "used" in parsed:
        parsed["free"] = max(0, parsed["total"] - parsed["used"])
    return parsed


def _parse_iso_timestamp(ts_str: str) -> tuple[str, float]:
    if not ts_str:
        return "", 0.0
    try:
        candidate = ts_str[:-1] + "+00:00" if ts_str.endswith("Z") else ts_str
        dt = datetime.fromisoformat(candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_utc = dt.astimezone(timezone.utc)
        return dt_utc.strftime("%Y-%m-%d %H:%M:%S"), dt_utc.timestamp()
    except ValueError:
        return ts_str, 0.0


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


class GdriveCliDriveGateway:
    """DriveGateway backed by custom-OAuth `gdrive` CLI with logical-path <-> ID caching."""

    def __init__(
        self,
        *,
        gdrive_bin: Optional[str] = None,
        agy_bin: Optional[str] = None,
        timeout: float = 20.0,
        semaphore: Optional[asyncio.Semaphore] = None,
        id_cache_path: Optional[Path] = None,
        folder_id_seeds: Optional[dict[str, str]] = None,
        use_agy_bridge: Optional[bool] = None,
    ) -> None:
        explicit_bin = gdrive_bin is not None
        self.gdrive_bin = gdrive_bin or _resolve_real_gdrive_bin()
        self.agy_bin = agy_bin or resolve_binary(
            "agy",
            Path.home() / ".local" / "bin" / "agy",
            "/usr/local/bin/agy",
            "/opt/homebrew/bin/agy",
        )
        self.timeout = timeout
        self.semaphore = semaphore
        self.id_cache_path = id_cache_path
        self._id_cache: dict[str, str] = {}
        self._load_id_cache()
        if folder_id_seeds:
            for k, v in folder_id_seeds.items():
                norm_k = self._norm_path(k)
                if norm_k and v:
                    self._id_cache.setdefault(norm_k, str(v))

        if use_agy_bridge is not None:
            self._use_agy_bridge = use_agy_bridge
        elif explicit_bin or self._has_standalone_creds():
            self._use_agy_bridge = False
        else:
            self._use_agy_bridge = self._has_agy_plugin_bridge()

    @staticmethod
    def _norm_path(remote_path: str) -> str:
        return "/".join(seg for seg in (remote_path or "").strip("/").split("/") if seg)

    @staticmethod
    def _has_standalone_creds() -> bool:
        if os.environ.get("GCLI_ACCESS_TOKEN"):
            return True
        return (Path.home() / ".config" / "gcli").exists() or (
            Path.home() / ".config" / "gdrive"
        ).exists()

    def _has_agy_plugin_bridge(self) -> bool:
        agy_p = Path(self.agy_bin).expanduser()
        has_agy = (agy_p.is_file() and os.access(agy_p, os.X_OK)) or bool(shutil.which(self.agy_bin))
        if not has_agy:
            return False
        return (
            (Path.home() / ".gemini" / "config" / "plugins" / "gdrive").exists()
            or (Path.home() / ".gemini" / "antigravity-cli" / "plugin_data" / "gdrive").exists()
        )

    def _load_id_cache(self) -> None:
        if self.id_cache_path is None or not self.id_cache_path.exists():
            return
        try:
            data = json.loads(self.id_cache_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, str) and k and v:
                    self._id_cache[self._norm_path(k)] = v

    def _save_id_cache(self) -> None:
        if self.id_cache_path is None:
            return
        try:
            self.id_cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.id_cache_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._id_cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.id_cache_path)
        except Exception:
            pass

    def is_configured(self) -> bool:
        """Return True when `gdrive` binary and local/Antigravity OAuth credentials exist."""
        bin_path = Path(self.gdrive_bin).expanduser()
        has_bin = (bin_path.is_file() and os.access(bin_path, os.X_OK)) or bool(
            shutil.which(self.gdrive_bin)
        )
        if not has_bin:
            return False
        if self._has_standalone_creds():
            return True
        return self._use_agy_bridge and self._has_agy_plugin_bridge()

    def capabilities(self) -> dict[str, Any]:
        return {
            "backend": "gdrive_cli",
            "configured": self.is_configured(),
            "read": True,
            "write": True,
            "readonly": False,
            "shared_client_id": False,
            "warnings": [],
        }

    def _build_argv(self, *args: str) -> list[str]:
        if self._use_agy_bridge:
            return [self.agy_bin, "plugin-cli", "gdrive", *args]
        return [self.gdrive_bin, *args]

    async def _run_gdrive(self, *args: str) -> str:
        res = await run_exec(
            self._build_argv(*args),
            semaphore=self.semaphore,
            timeout=self.timeout,
            env=_sanitized_gdrive_env(self.gdrive_bin),
        )
        if res.timed_out or res.returncode != 0:
            msg = (res.stderr or res.stdout).strip() or f"gdrive {' '.join(args[:2])} failed ({res.returncode})"
            raise RuntimeError(redact_secrets(msg))
        return res.stdout

    async def _ls_raw(self, folder_id: str) -> list[dict[str, Any]]:
        args = ["readonly", "ls"]
        if folder_id and folder_id != "root":
            args.append(folder_id)
        args.append("--json")
        out = await self._run_gdrive(*args)
        try:
            data = json.loads(out or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"gdrive ls JSON decode error: {exc}") from exc
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    async def _search_folder_id(self, name: str) -> Optional[str]:
        out = await self._run_gdrive(
            "readonly",
            "search",
            "--name-exact",
            name,
            "--type",
            "folder",
            "--json",
        )
        try:
            hits = json.loads(out or "[]")
        except json.JSONDecodeError:
            return None
        if isinstance(hits, list) and hits and isinstance(hits[0], dict):
            fid = hits[0].get("id")
            return str(fid) if fid else None
        return None

    def _invalidate_path(self, norm_path: str) -> None:
        if not norm_path:
            return
        prefix = norm_path + "/"
        stale_keys = [k for k in self._id_cache if k == norm_path or k.startswith(prefix)]
        for k in stale_keys:
            self._id_cache.pop(k, None)

    async def _resolve_id(self, remote_path: str) -> str:
        norm = self._norm_path(remote_path)
        if not norm or norm == "root":
            return "root"
        if norm in self._id_cache:
            return self._id_cache[norm]

        parts = norm.split("/")
        cur_id = "root"
        start_idx = 0
        for idx in range(len(parts) - 1, 0, -1):
            prefix = "/".join(parts[:idx])
            if prefix in self._id_cache:
                cur_id = self._id_cache[prefix]
                start_idx = idx
                break

        dirty = False
        for idx in range(start_idx, len(parts)):
            seg = parts[idx]
            cur_prefix = "/".join(parts[: idx + 1])
            entries = await self._ls_raw(cur_id)
            matched_id: Optional[str] = None
            for item in entries:
                iname = str(item.get("name", ""))
                iid = str(item.get("id", ""))
                if not iname or not iid:
                    continue
                child_path = f"{'/'.join(parts[:idx])}/{iname}" if idx > 0 else iname
                self._id_cache[child_path] = iid
                dirty = True
                if iname == seg:
                    matched_id = iid
            if matched_id is None and idx == 0:
                matched_id = await self._search_folder_id(seg)
                if matched_id:
                    self._id_cache[cur_prefix] = matched_id
                    dirty = True
            if matched_id is None:
                if dirty:
                    self._save_id_cache()
                raise FileNotFoundError(f"Drive path not found: {norm}")
            cur_id = matched_id

        if dirty:
            self._save_id_cache()
        return cur_id

    async def cat_text(self, remote_path: str) -> str:
        norm = self._norm_path(remote_path)
        for attempt in range(2):
            file_id = await self._resolve_id(norm)
            try:
                with tempfile.TemporaryDirectory() as td:
                    dst = Path(td) / "drive_file.txt"
                    await self._run_gdrive("readonly", "download", file_id, "--out", str(dst))
                    return dst.read_text(encoding="utf-8", errors="replace")
            except Exception:
                if attempt == 0 and norm in self._id_cache:
                    self._invalidate_path(norm)
                    continue
                raise
        raise FileNotFoundError(f"Drive file not found: {norm}")

    async def _list_folder_entries(self, remote_path: str) -> list[dict[str, Any]]:
        norm = self._norm_path(remote_path)
        for attempt in range(2):
            folder_id = await self._resolve_id(norm)
            try:
                entries = await self._ls_raw(folder_id)
                dirty = False
                for item in entries:
                    iname = str(item.get("name", ""))
                    iid = str(item.get("id", ""))
                    if iname and iid:
                        child_key = f"{norm}/{iname}" if norm else iname
                        if self._id_cache.get(child_key) != iid:
                            self._id_cache[child_key] = iid
                            dirty = True
                if dirty:
                    self._save_id_cache()
                return entries
            except Exception:
                if attempt == 0 and norm in self._id_cache:
                    self._invalidate_path(norm)
                    continue
                raise
        return []

    async def lsf(self, remote_path: str, dirs_only: bool = False) -> list[str]:
        entries = await self._list_folder_entries(remote_path)
        names: list[str] = []
        for item in entries:
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            if dirs_only and item.get("mimeType") != _FOLDER_MIME:
                continue
            names.append(name)
        return names

    async def lsl(self, remote_path: str, max_depth: int = 1) -> list[dict[str, Any]]:
        norm = self._norm_path(remote_path)
        results: list[dict[str, Any]] = []

        async def _walk(cur_path: str, rel_prefix: str, depth_left: int) -> None:
            entries = await self._list_folder_entries(cur_path)
            subdirs: list[tuple[str, str]] = []
            for item in entries:
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                if item.get("mimeType") == _FOLDER_MIME:
                    if depth_left > 1:
                        sub_rel = f"{rel_prefix}/{name}" if rel_prefix else name
                        sub_path = f"{cur_path}/{name}" if cur_path else name
                        subdirs.append((sub_path, sub_rel))
                    continue
                mod_str, mtime_ep = _parse_iso_timestamp(str(item.get("modifiedTime", "")))
                results.append(
                    {
                        "name": f"{rel_prefix}/{name}" if rel_prefix else name,
                        "size": int(item.get("size") or 0),
                        "modified_at": mod_str,
                        "mtime_epoch": mtime_ep,
                    }
                )
            for sub_path, sub_rel in subdirs:
                await _walk(sub_path, sub_rel, depth_left - 1)

        await _walk(norm, "", max(1, int(max_depth)))
        return results

    async def about(self) -> dict[str, Any]:
        out = await self._run_gdrive("readonly", "quota", "--json")
        return parse_gdrive_quota(out)


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
            "backend": "rclone",
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
            raise RuntimeError(redact_secrets(res.stderr.strip() or f"rclone cat failed ({res.returncode})"))
        return res.stdout

    async def lsf(self, remote_path: str, dirs_only: bool = False) -> list[str]:
        argv = ["lsf"]
        if dirs_only:
            argv.append("--dirs-only")
        argv.append(self._remote_spec(remote_path))
        res = await run_exec(self._rclone_cmd(*argv), semaphore=self.semaphore, timeout=self.timeout)
        if res.timed_out or res.returncode != 0:
            raise RuntimeError(redact_secrets(res.stderr.strip() or f"rclone lsf failed ({res.returncode})"))
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]

    async def lsl(self, remote_path: str, max_depth: int = 1) -> list[dict[str, Any]]:
        res = await run_exec(
            self._rclone_cmd("lsl", "--max-depth", str(max_depth), self._remote_spec(remote_path)),
            semaphore=self.semaphore,
            timeout=self.timeout,
        )
        if res.timed_out or res.returncode != 0:
            raise RuntimeError(redact_secrets(res.stderr.strip() or f"rclone lsl failed ({res.returncode})"))
        return parse_rclone_lsl(res.stdout)

    async def about(self) -> dict[str, Any]:
        res = await run_exec(
            self._rclone_cmd("about", f"{self.remote}:", "--json"),
            semaphore=self.semaphore,
            timeout=self.timeout,
        )
        if res.timed_out or res.returncode != 0:
            raise RuntimeError(redact_secrets(res.stderr.strip() or f"rclone about failed ({res.returncode})"))
        return json.loads(res.stdout)


def create_drive_gateway(
    preferred: str = "auto",
    *,
    remote: str = "gdrive",
    semaphore: Optional[asyncio.Semaphore] = None,
    id_cache_path: Optional[Path] = None,
) -> DriveGateway:
    """Select `GdriveCliDriveGateway` or `RcloneDriveGateway` according to preference & local auth."""
    choice = (preferred or "auto").strip().lower()
    if choice == "rclone":
        return RcloneDriveGateway(remote=remote, semaphore=semaphore)
    gdrive_gw = GdriveCliDriveGateway(semaphore=semaphore, id_cache_path=id_cache_path)
    if choice == "gdrive":
        return gdrive_gw
    if gdrive_gw.is_configured():
        return gdrive_gw
    return RcloneDriveGateway(remote=remote, semaphore=semaphore)
