"""System 4 and the outbound article (ADR 023, T5.1; ADRs 003, 012).

Four stages in the order they feed each other: scan (no LLM, network), briefing,
content strategy, article draft (System 1D). Each stage lists its past outputs, newest
first, and its action works on the selected output of the stage before it, which is the
newest unless the operator picks another. "Scan and brief" runs the first two in
sequence and does not start the briefing if the scan fails.
"""
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Static

from tui.screens.base import BaseScreen
from tui.screens.run import RunScreen
from tui.services import commands, estimate
from tui.services.command_model import build_model
from tui.services.files import dated_outputs, open_path
from tui.services.session import RunRequest
from tui.widgets.command_hint import CommandHint
from tui.widgets.run_button import RunButton

FIRST_SCAN_WARNING = ("This is the first scan. It lists many old items from every source; later scans show only what "
                      "is new since the previous one (ADR 003). It fetches your watchlist over the network and makes no "
                      "LLM call.")

# table id -> (system, stage, command path that produces it, heading)
STAGES = {
    "scans": ("s4", "scan", ["s4", "scan"], "Scans"),
    "briefings": ("s4", "briefing", ["s4", "briefing"], "Briefings"),
    "strategies": ("s4", "content-strategy", ["s4", "content-strategy"], "Content strategies"),
    "drafts": ("s1d", "article-draft", ["s1d", "article-draft"], "Article drafts"),
}
# the stage whose selected output feeds each action: button id -> (table the input comes from, label start, missing reason)
ACTIONS = {
    "run-briefing": ("scans", "Brief scan", "Run a scan first"),
    "run-strategy": ("briefings", "Content strategy from briefing", "Run a briefing first"),
    "run-draft": ("strategies", "Draft article from strategy", "Run a content strategy first"),
}
TARGETS = {"run-briefing": "briefings", "run-strategy": "strategies", "run-draft": "drafts"}


class IntelligenceScreen(BaseScreen):
    def __init__(self):
        super().__init__()
        self.outputs: dict = {}      # table id -> list of files.DatedOutput, newest first
        self._specs: dict = {}

    @property
    def root(self):
        return self.app.ctx.data_dir("intelligence_dir")

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(id="intel-scroll"):
            yield Static("", id="last-scan", markup=False)
            for table_id, (_system, _stage, _path, heading) in STAGES.items():
                yield Static(heading, classes="stage-heading", markup=False)
                yield DataTable(id=table_id, cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="stage-actions"):
                    if table_id == "scans":
                        yield RunButton("Scan now", id="run-scan")
                        yield RunButton("Scan and brief", id="run-both")
                    else:
                        yield RunButton("Run", id=next(b for b, t in TARGETS.items() if t == table_id))
                    yield Button("Open selected", id=f"open-{table_id[:-1] if table_id != 'strategies' else 'strategy'}")
            yield CommandHint(id="intel-hint")

    def on_mount(self) -> None:
        model = {tuple(s.path): s for s in build_model(self.app.root)}
        self._specs = {table_id: model[tuple(path)] for table_id, (_s, _st, path, _h) in STAGES.items()}
        for table_id in STAGES:
            self.query_one(f"#{table_id}", DataTable).add_columns("Date", "File")
        self.refresh_lists()

    def on_screen_resume(self) -> None:
        self.refresh_lists()

    def run_finished(self, result) -> None:
        self.refresh_lists()

    # -- the lists --------------------------------------------------------------------

    def refresh_lists(self) -> None:
        for table_id, (system, stage, _path, _heading) in STAGES.items():
            table = self.query_one(f"#{table_id}", DataTable)
            previous = table.cursor_row
            table.clear()
            self.outputs[table_id] = dated_outputs(self.root, system, stage)
            for out in self.outputs[table_id]:
                table.add_row(out.date, str(out.path.relative_to(self.root)))
            if table.row_count:
                table.move_cursor(row=min(previous, table.row_count - 1))
        scans = self.outputs["scans"]
        self.query_one("#last-scan", Static).update(
            f"Last scan: {scans[0].date}" if scans else "No scan yet. The first one surfaces many old items.")
        self._update_buttons()

    def selected(self, table_id: str):
        """The `files.DatedOutput` under the cursor in a list, or None if it is empty."""
        rows = self.outputs.get(table_id, [])
        index = self.query_one(f"#{table_id}", DataTable).cursor_row
        return rows[index] if rows and 0 <= index < len(rows) else None

    @on(DataTable.RowHighlighted)
    def _cursor_moved(self) -> None:
        self._update_buttons()

    def _update_buttons(self) -> None:
        for button_id, (source, label, missing) in ACTIONS.items():
            chosen = self.selected(source)
            button = self.query_one(f"#{button_id}", RunButton)
            button.label = f"{label} {chosen.date}" if chosen else label
            button.blocked_reason = None if chosen else missing
        for table_id in STAGES:
            open_id = f"#open-{table_id[:-1] if table_id != 'strategies' else 'strategy'}"
            self.query_one(open_id, Button).disabled = self.selected(table_id) is None

    # -- opening -------------------------------------------------------------------------

    def _open(self, table_id: str) -> None:
        chosen = self.selected(table_id)
        if chosen is not None:
            open_path(chosen.path)

    @on(Button.Pressed, "#open-scan")
    def _open_scan(self) -> None:
        self._open("scans")

    @on(Button.Pressed, "#open-briefing")
    def _open_briefing(self) -> None:
        self._open("briefings")

    @on(Button.Pressed, "#open-strategy")
    def _open_strategy(self) -> None:
        self._open("strategies")

    @on(Button.Pressed, "#open-draft")
    def _open_draft(self) -> None:
        self._open("drafts")

    # -- running ----------------------------------------------------------------------------

    def _request(self, table_id: str, input_path=None, **kwargs) -> RunRequest:
        system, stage, path, _heading = STAGES[table_id]
        values = {"input_file": str(input_path)} if input_path is not None else {}
        argv = commands.argv_for(self.app.ctx, self._specs[table_id], values)
        guess = estimate.estimate_task(self.app.ctx, system, stage, input_path) if input_path is not None else None
        return RunRequest(" ".join(path), argv, estimate=guess, **kwargs)

    def _scan_request(self, **kwargs) -> RunRequest:
        warnings = [] if self.outputs["scans"] else [FIRST_SCAN_WARNING]
        return self._request("scans", warnings=warnings, **kwargs)

    @on(RunButton.Pressed, "#run-scan")
    def _run_scan(self) -> None:
        self.app.begin_run(self._scan_request())

    @on(RunButton.Pressed, "#run-both")
    def _run_scan_and_brief(self) -> None:
        self.app.begin_run(self._scan_request(on_done=self._after_scan))

    def _after_scan(self, result) -> None:
        """The briefing follows the scan only if the scan succeeded; a failed or cancelled
        scan's log stays on screen."""
        if result.returncode != 0 or result.cancelled:
            return
        newest = dated_outputs(self.root, "s4", "scan")
        if not newest:
            return
        if isinstance(self.app.screen, RunScreen):
            self.app.pop_screen()   # replace the finished scan's log with the briefing's confirmation
        self.app.begin_run(self._request("briefings", newest[0].path))

    def _run_stage(self, button_id: str) -> None:
        source = ACTIONS[button_id][0]
        chosen = self.selected(source)
        if chosen is not None:
            self.app.begin_run(self._request(TARGETS[button_id], chosen.path))

    @on(RunButton.Pressed, "#run-briefing")
    def _run_briefing(self) -> None:
        self._run_stage("run-briefing")

    @on(RunButton.Pressed, "#run-strategy")
    def _run_strategy(self) -> None:
        self._run_stage("run-strategy")

    @on(RunButton.Pressed, "#run-draft")
    def _run_draft(self) -> None:
        self._run_stage("run-draft")
