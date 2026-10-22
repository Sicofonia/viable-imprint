"""One command run, as the UI sees it (ADR 023). Plain Python, no Textual.

A `RunRequest` says what to run and what to tell the operator first. A
`RunSession` runs it through the app's single `TaskRunner` and keeps the output
so a view can attach, detach and re-attach while it runs (a translation can take
an hour).
"""
import asyncio
from dataclasses import dataclass, field
from typing import Callable

from tui.services.runner import RunResult, TaskRunner


@dataclass
class RunRequest:
    title: str
    argv: list
    estimate: object = None                    # services.estimate.Estimate, or None
    warnings: list = field(default_factory=list)   # shown before running, e.g. "this overwrites ..."
    confirm: bool = False                      # ask even when there is no estimate (e.g. --update)

    @property
    def needs_confirmation(self) -> bool:
        return bool(self.estimate or self.warnings or self.confirm)


class RunSession:
    def __init__(self, request: RunRequest, runner: TaskRunner):
        self.request = request
        self.runner = runner
        self.lines: list = []
        self.result: RunResult = None
        self._listeners: list = []
        self._task: asyncio.Task = None

    def subscribe(self, listener: Callable[[str, object], None]) -> None:
        """`listener(event, payload)`: ("line", text) for each line, ("done", RunResult) once."""
        self._listeners.append(listener)

    def unsubscribe(self, listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    @property
    def running(self) -> bool:
        return self.result is None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def wait(self) -> RunResult:
        await self._task
        return self.result

    async def cancel(self, grace_seconds: float = 10) -> None:
        await self.runner.cancel(grace_seconds)

    def _emit(self, event: str, payload) -> None:
        for listener in list(self._listeners):
            listener(event, payload)

    def _on_line(self, line: str) -> None:
        self.lines.append(line)
        self._emit("line", line)

    async def _run(self) -> None:
        try:
            self.result = await self.runner.run(self.request.argv, self._on_line)
        except Exception as exc:  # noqa: BLE001 - the view must always learn that the run ended
            self.result = RunResult(argv=self.request.argv, lines=[f"The run could not be started: {exc}"])
        self._emit("done", self.result)
