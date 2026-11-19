"""Rex terminal multiplexer gateway adapter."""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any, Optional, Sequence

from .exec_util import run_exec


class RexGateway:
    """Wrapper around the `rex` CLI for block topology, inspection, and dock lifecycle."""

    def __init__(
        self,
        *,
        rex_bin: Optional[str] = None,
        timeout: float = 5.0,
        semaphore: Optional[asyncio.Semaphore] = None,
    ) -> None:
        self.rex_bin = rex_bin or shutil.which("rex") or "rex"
        self.timeout = timeout
        self.semaphore = semaphore

    async def whoami(self) -> dict[str, Any]:
        res = await run_exec(
            [self.rex_bin, "whoami", "--json"],
            semaphore=self.semaphore,
            timeout=self.timeout,
        )
        if res.timed_out or res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or "rex whoami failed")
        return json.loads(res.stdout)

    async def list_blocks(self) -> list[dict[str, Any]]:
        res = await run_exec(
            [self.rex_bin, "block", "list", "--json"],
            semaphore=self.semaphore,
            timeout=self.timeout,
        )
        if res.timed_out or res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or "rex block list failed")
        data = json.loads(res.stdout)
        return list(data.get("blocks", []))

    async def get_block_process(self, block_id: str) -> dict[str, Any]:
        res = await run_exec(
            [self.rex_bin, "do", "-b", block_id, "com.superlogical.terminal.process"],
            semaphore=self.semaphore,
            timeout=self.timeout,
        )
        if res.timed_out or res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or f"rex do process failed for {block_id}")
        return json.loads(res.stdout)

    async def split_dock(
        self,
        anchor_block_id: str,
        command: Sequence[str],
        *,
        ratio: int = 22,
        label: str = "faiz-dash",
    ) -> dict[str, Any]:
        argv = [
            self.rex_bin,
            "split",
            "-b",
            anchor_block_id,
            "--split=below",
            "--ratio",
            str(ratio),
            "--focus=false",
            "--label",
            label,
            "--shell=none",
            "--json",
            "--",
            *command,
        ]
        res = await run_exec(argv, semaphore=self.semaphore, timeout=self.timeout)
        if res.timed_out or res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or "rex split failed")
        return json.loads(res.stdout) if res.stdout.strip() else {}

    async def close_block(self, block_id: str) -> bool:
        res = await run_exec(
            [self.rex_bin, "block", "close", block_id],
            semaphore=self.semaphore,
            timeout=self.timeout,
        )
        return not res.timed_out and res.returncode == 0
