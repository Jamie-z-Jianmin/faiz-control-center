"""Shared semaphore-bounded async subprocess execution helper."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
from typing import Mapping, Optional, Sequence


def resolve_binary(name: str, *fallbacks: str | Path) -> str:
    """Resolve an executable via PATH first, then fall back to known install locations."""
    found = shutil.which(name)
    if found:
        return found
    for cand in fallbacks:
        p = Path(cand).expanduser()
        if os.access(p, os.X_OK):
            return str(p)
    return str(Path(fallbacks[0]).expanduser()) if fallbacks else name


@dataclass(frozen=True)
class CommandResult:
    """Captured result from an async subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


async def run_exec(
    argv: Sequence[str],
    *,
    semaphore: Optional[asyncio.Semaphore] = None,
    timeout: float = 12.0,
    env: Optional[Mapping[str, str]] = None,
) -> CommandResult:
    """Run an executable without shell, bounded by semaphore and hard timeout."""

    async def _invoke() -> CommandResult:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env) if env is not None else None,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return CommandResult(
                returncode=proc.returncode or 0,
                stdout=out_b.decode("utf-8", errors="replace"),
                stderr=err_b.decode("utf-8", errors="replace"),
                timed_out=False,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except Exception:
                pass
            return CommandResult(
                returncode=-1,
                stdout="",
                stderr=f"Command timed out after {timeout:.1f}s",
                timed_out=True,
            )

    if semaphore is None:
        return await _invoke()
    async with semaphore:
        return await _invoke()
