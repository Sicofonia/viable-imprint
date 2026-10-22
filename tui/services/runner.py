"""Runs one `pipeline.py` command at a time as an async subprocess (ADR 023).

Output is streamed line by line; cancelling interrupts the child the way Ctrl+C
would, so `task_loader._run_recorded()` records the task as `failed`, and only
terminates it if it has not stopped after a grace period. The ledger has no file
locking, so a second run while one is in progress is refused (Decision 2).

Plain Python, no Textual. All methods must be used from the one event loop that
calls `run()`.
"""
import asyncio
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from lib import interrupt

# One output line may be long (a whole paragraph echoed back); asyncio's default
# 64 KiB reader limit would raise on it.
_LINE_LIMIT = 16 * 1024 * 1024
_KILL_AFTER_TERMINATE = 5.0


class RunnerBusy(RuntimeError):
    """`run()` was called while another command is still running."""


@dataclass
class RunResult:
    argv: list
    returncode: int = None      # None when the process could not be started
    cancelled: bool = False
    forced: bool = False        # True if terminate was needed after the graceful interrupt
    lines: list = field(default_factory=list)
    seconds: float = 0.0


class TaskRunner:
    def __init__(self, cwd: Path = None):
        self.cwd = Path(cwd) if cwd else None
        self._proc = None
        self._busy = False
        self._cancel_requested = False
        self._forced = False

    @property
    def busy(self) -> bool:
        return self._busy

    def _environment(self) -> dict:
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        if sys.platform == "win32":
            env[interrupt.ENV_VAR] = "1"  # see lib/interrupt.py
        return env

    async def run(self, argv: list, on_line: Callable[[str], None]) -> RunResult:
        if self._busy:
            raise RunnerBusy("another command is still running")
        self._busy = True
        self._cancel_requested = self._forced = False
        result = RunResult(argv=list(argv))
        started = time.monotonic()
        try:
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    cwd=self.cwd, env=self._environment(), limit=_LINE_LIMIT, creationflags=flags,
                )
            except OSError as exc:
                line = f"Could not start the command: {exc}"
                result.lines.append(line)
                on_line(line)
                return result

            try:
                async for raw in self._proc.stdout:
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    result.lines.append(line)
                    on_line(line)
                result.returncode = await self._proc.wait()
            finally:
                if self._proc.returncode is None:  # this task was cancelled from outside
                    self._proc.kill()
                    await self._proc.wait()
            result.cancelled = self._cancel_requested
            result.forced = self._forced
            return result
        finally:
            result.seconds = round(time.monotonic() - started, 2)
            self._proc = None
            self._busy = False

    async def cancel(self, grace_seconds: float = 10) -> None:
        """Interrupt the running command; terminate it if it is still alive after
        `grace_seconds`. Returns once it has stopped. A no-op when nothing runs."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        self._cancel_requested = True
        try:
            # Windows: the child runs in its own process group (see run()) and
            # lib/interrupt.py turns this into a KeyboardInterrupt.
            proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
        except (OSError, ProcessLookupError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), grace_seconds)
            return
        except asyncio.TimeoutError:
            pass
        self._forced = True
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), _KILL_AFTER_TERMINATE)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
