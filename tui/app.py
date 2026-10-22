"""PipelineApp: screen routing, key bindings, command palette (ADR 023)."""
from functools import partial

import click
from textual.app import App, SystemCommand
from textual.binding import Binding
from textual.reactive import reactive
from textual.screen import ModalScreen

from tui.screens import registry
from tui.screens.help import HelpScreen, binding_lines
from tui.screens.home import HomeScreen
from tui.screens.run import RunScreen, summarize
from tui.services.context import Context
from tui.services.runner import TaskRunner
from tui.services.session import RunRequest, RunSession


class PipelineApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "Viable Imprint"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("ctrl+r", "show_run", "Run log"),
    ]

    # True while a command runs; every button that starts one watches it.
    busy = reactive(False)

    def __init__(self, ctx: Context, root: click.Group):
        super().__init__()
        self.ctx = ctx
        self.root = root
        self.runner = TaskRunner(cwd=ctx.repo_root)
        self.session: RunSession = None

    def on_mount(self) -> None:
        self.push_screen(HomeScreen())

    # -- navigation -----------------------------------------------------

    def go_home(self) -> None:
        while not isinstance(self.screen, HomeScreen) and len(self.screen_stack) > 2:
            self.pop_screen()

    def open_screen(self, name: str) -> None:
        self.go_home()
        self.push_screen(registry.entry(name).create())

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        for e in registry.ENTRIES:
            yield SystemCommand(f"Open: {e.title}", e.description, partial(self.open_screen, e.name))
        yield SystemCommand("Show help", "Keys and the one-run-at-a-time rule", self.action_help)
        yield SystemCommand("Show the run log", "Reopen the running or last command's log", self.action_show_run)

    def action_help(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self.push_screen(HelpScreen(binding_lines([self.screen], self)))

    # -- running commands -----------------------------------------------

    def begin_run(self, request: RunRequest) -> None:
        """Open the run modal for `request` (confirmation first when it has an estimate or warnings)."""
        if self.runner.busy:
            self.notify("Another command is running. One run at a time.", severity="warning")
            self.action_show_run()
            return
        self.push_screen(RunScreen(request))

    def start_session(self, request: RunRequest) -> RunSession:
        session = RunSession(request, self.runner)
        self.session = session
        self.busy = True
        self.sub_title = f"Running: {request.title}"
        session.subscribe(self._on_session_event)
        session.start()
        return session

    def _on_session_event(self, event: str, payload) -> None:
        if event != "done":
            return
        self.busy = False
        self.sub_title = ""
        title = self.session.request.title
        self.notify(f"{title}: {summarize(payload)}",
                    severity="information" if payload.returncode == 0 and not payload.cancelled else "warning")
        if self.session.request.on_done is not None:
            try:
                self.session.request.on_done(payload)
            except Exception as exc:  # noqa: BLE001 - a follow-up step failing must not break the event loop
                self.notify(f"{title}: the follow-up step failed: {exc}", severity="error")
        for screen in list(self.screen_stack):
            handler = getattr(screen, "run_finished", None)
            if handler is not None:
                handler(payload)

    def open_book(self, slug: str) -> None:
        from tui.screens.book import BookScreen  # imported on use: screens import the app's helpers
        self.go_home()
        self.push_screen(BookScreen(slug))

    def action_show_run(self) -> None:
        if self.session is None:
            self.notify("Nothing has been run yet.")
            return
        if isinstance(self.screen, RunScreen):
            return
        self.push_screen(RunScreen(session=self.session))


def run_app(root: click.Group, config_path: str) -> None:
    """Called by `pipeline.py ui` with its own Click group and `--config` value."""
    PipelineApp(Context.load(config_path), root).run()
