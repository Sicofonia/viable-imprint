"""The run modal: confirmation, streamed log, cancel, summary (ADR 023, T1.4).

Every action that runs a command goes through here. The modal is only a view on
a `RunSession` owned by the app: hiding it leaves the run going, and `ctrl+r`
opens it again.
"""
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, RichLog, Static

from tui.services.files import open_path, parse_saved
from tui.services.output import chain_summary
from tui.services.session import RunRequest, RunSession
from tui.widgets.command_hint import CommandHint


def summarize(result) -> str:
    """One paragraph on how a run ended, in terms of what the ledger now holds."""
    seconds = f"{result.seconds:.1f} s"
    if result.returncode is None:
        return "The command could not be started."
    if result.cancelled and result.forced:
        return (f"Cancelled after {seconds}. The process did not stop on its own and was terminated, so the "
                "run-state ledger was not updated: the task still shows whatever it showed before.")
    if result.cancelled:
        return (f"Cancelled after {seconds}. The interrupt was recorded in the ledger as a failed attempt, "
                "which can be retried.")
    if result.returncode == 0:
        return f"Finished: exit code 0 in {seconds}."
    return (f"Failed: exit code {result.returncode} after {seconds}. Read the log above; a task that failed "
            "is recorded in the ledger and can be run again.")


def confirmation_text(request: RunRequest) -> str:
    parts = []
    if request.warnings:
        parts.append("\n".join(f"! {w}" for w in request.warnings))
    if request.estimate is not None:
        parts.append("This will call a provider:\n" + "\n".join(f"  {line}" for line in request.estimate.lines()))
    if not parts:
        parts.append("Run this command?")
    return "\n\n".join(parts)


class RunScreen(ModalScreen):
    BINDINGS = [Binding("escape", "leave", "Hide")]

    def __init__(self, request: RunRequest = None, session: RunSession = None):
        super().__init__()
        self.request = request if request is not None else session.request
        self.session = session
        self.state = "confirm"          # confirm | running | done

    def compose(self) -> ComposeResult:
        with Vertical(id="run-dialog"):
            yield Label(self.request.title, id="run-title")
            yield CommandHint(id="run-hint")
            yield Static("", id="run-confirm", markup=False)
            yield RichLog(id="run-log", wrap=True, markup=False, highlight=False)
            yield Static("", id="run-summary", markup=False)
            with Horizontal(id="run-buttons"):
                yield Button("Run", id="run-go", variant="primary")
                yield Button("Cancel run", id="run-cancel", variant="error")
                yield Button("Open output", id="run-open")
                yield Button("Hide", id="run-hide")
                yield Button("Close", id="run-close")

    def on_mount(self) -> None:
        self.query_one(CommandHint).show_argv(self.request.argv)
        if self.session is not None:
            self._follow_session()
        elif self.request.needs_confirmation:
            self._show_state("confirm")
        else:
            self._start_run()

    def on_unmount(self) -> None:
        if self.session is not None:
            self.session.unsubscribe(self._on_session_event)

    # -- states ---------------------------------------------------------

    def _show_state(self, state: str) -> None:
        self.state = state
        confirm, log, summary = (self.query_one(f"#run-{n}") for n in ("confirm", "log", "summary"))
        confirm.display = state == "confirm"
        log.display = state != "confirm"
        summary.display = state == "done"
        visible = {"confirm": ("run-go", "run-close"), "running": ("run-cancel", "run-hide"),
                   "done": ("run-open", "run-close")}[state]
        for button_id in ("run-go", "run-cancel", "run-open", "run-hide", "run-close"):
            self.query_one(f"#{button_id}").display = button_id in visible
        self.query_one("#run-close", Button).label = "Cancel" if state == "confirm" else "Close"
        if state == "confirm":
            confirm.update(confirmation_text(self.request))
        if state == "done":
            self.query_one("#run-open").display = bool(parse_saved(self.session.lines))

    def _start_run(self) -> None:
        self.session = self.app.start_session(self.request)
        self._follow_session()

    def _follow_session(self) -> None:
        log = self.query_one(RichLog)
        for line in self.session.lines:
            log.write(line)
        if self.session.result is not None:
            self._show_result(self.session.result)
        else:
            self._show_state("running")
            self.session.subscribe(self._on_session_event)

    def _on_session_event(self, event: str, payload) -> None:
        if event == "line":
            self.query_one(RichLog).write(payload)
        else:
            self._show_result(payload)

    def _show_result(self, result) -> None:
        self.session.unsubscribe(self._on_session_event)
        text = summarize(result)
        chain = chain_summary(self.session.lines)
        if chain is not None:
            text += "\n" + chain.sentence()
        self.query_one("#run-summary", Static).update(text)
        self._show_state("done")

    # -- actions --------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button.id
        if button == "run-go":
            self._start_run()
        elif button == "run-cancel":
            event.button.disabled = True
            event.button.label = "Cancelling..."
            self.run_worker(self.session.cancel(), exclusive=True)
        elif button == "run-open":
            saved = parse_saved(self.session.lines)
            if saved:
                open_path(saved[-1])
        elif button in ("run-hide", "run-close"):
            self.action_leave()

    def action_leave(self) -> None:
        self.dismiss(self.session.result if self.session is not None else None)
