"""System 5: candidates under policy evaluation (ADR 023, Phase 7; ADRs 006, 013).

Every brief in `candidates/s1a/briefs/` with where it stands (not evaluated, evaluated with
the agent's verdict, decided), a way to start a new one, evaluate it, open the verdict, record
the Director's own decision against it, and how often each verdict has ended in each decision.
Advisory only: nothing here decides anything. Briefs open in the operator's editor.
"""
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Input, Select, Static

from tui.screens.base import BaseScreen
from tui.services import commands, estimate, files, state
from tui.services.command_model import build_model
from tui.services.files import open_path
from tui.services.session import RunRequest
from tui.widgets.command_hint import CommandHint
from tui.widgets.run_button import RunButton

BRIEF_REMINDER = (
    "Writing a brief (ADR 006): plain prose, not a form. Useful to include: title, author, approximate date, "
    "language, a paragraph on subject and scope, what is known about copyright status, and notes on the quality "
    "and availability of the source. Avoid `Label: value` fields: the evaluator tends to answer in the same style "
    "and drops its headings (ADR 006, point 9)."
)
DECISIONS = [("acquired", "acquired"), ("declined", "declined"), ("deferred", "deferred")]


class CandidatesScreen(BaseScreen):
    def __init__(self):
        super().__init__()
        self.rows: dict = {}          # slug -> state.CandidateRow
        self._specs: dict = {}
        self._pending: dict = {}      # id(request) -> follow-up for when that run ends

    @property
    def root(self):
        return self.app.ctx.data_dir("candidates_dir")

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(id="cand-scroll"):
            yield Static("", id="cand-policy", markup=False)
            yield Static(BRIEF_REMINDER, id="cand-reminder", markup=False)
            yield DataTable(id="cand-list", cursor_type="row", zebra_stripes=True)
            with Horizontal(classes="cand-actions"):
                yield Button("Open brief", id="cand-open-brief")
                yield RunButton("Evaluate", id="cand-evaluate")
                yield Button("Open verdict", id="cand-open-verdict")
            yield CommandHint(id="cand-hint")
            yield Static("Record your decision", classes="section", markup=False)
            with Horizontal(classes="cand-actions"):
                yield Select(DECISIONS, id="cand-decision-select", prompt="Choose")
                yield Input(id="cand-decision-text", placeholder="or write your own")
                yield RunButton("Record decision", id="cand-record")
            yield Static("", id="cand-decision-error", markup=False)
            yield Static("New candidate", classes="section", markup=False)
            with Horizontal(classes="cand-actions"):
                yield Input(id="cand-new-slug", placeholder="a-short-name-for-the-candidate")
                yield RunButton("Create and open the brief", id="cand-new")
            yield Static("", id="cand-new-error", markup=False)
            yield Static("Verdict against decision", classes="section", markup=False)
            yield DataTable(id="cand-calibration", cursor_type="row")

    def on_mount(self) -> None:
        model = {tuple(s.path): s for s in build_model(self.app.root)}
        self._specs = {name: model[path] for name, path in (
            ("evaluate", ("s5", "evaluate")), ("record", ("candidate", "record-decision")),
            ("new", ("candidate", "new")))}
        self.query_one("#cand-list", DataTable).add_columns("Candidate", "State", "Verdict", "Decision", "Decided on")
        self.query_one("#cand-calibration", DataTable).add_columns("Agent verdict", "Your decision", "Times")
        self.refresh_state()

    def on_screen_resume(self) -> None:
        self.refresh_state()

    def run_finished(self, result) -> None:
        request = self.app.session.request if self.app.session is not None else None
        after = self._pending.pop(id(request), None)
        if after is not None:
            after(result)
        self.refresh_state()

    # -- the list ------------------------------------------------------------------------------

    def refresh_state(self) -> None:
        table = self.query_one("#cand-list", DataTable)
        previous = table.cursor_row
        table.clear()
        self.rows = {}
        for row in state.candidate_rows(self.root):
            self.rows[row.slug] = row
            table.add_row(row.slug, row.state, row.verdict or "-", row.decision or "-", row.decided_on or "-", key=row.slug)
        if table.row_count:
            table.move_cursor(row=min(previous, table.row_count - 1))

        calibration = self.query_one("#cand-calibration", DataTable)
        calibration.clear()
        for c in state.calibration_counts(self.root):
            calibration.add_row(c.verdict, c.decision, str(c.count))
        self._update_actions()

    def selected(self):
        table = self.query_one("#cand-list", DataTable)
        if not table.row_count:
            return None
        return self.rows.get(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    @on(DataTable.RowHighlighted, "#cand-list")
    def _cursor_moved(self) -> None:
        self._update_actions()

    def _update_actions(self) -> None:
        row = self.selected()
        self.query_one("#cand-open-brief", Button).disabled = row is None
        self.query_one("#cand-open-verdict", Button).disabled = row is None or row.verdict_file is None
        evaluate = self.query_one("#cand-evaluate", RunButton)
        record = self.query_one("#cand-record", RunButton)
        hint = self.query_one("#cand-hint", CommandHint)
        if row is None:
            evaluate.blocked_reason, record.blocked_reason = "Select a candidate", "Select a candidate"
            hint.clear()
            return
        evaluate.label = "Evaluate again" if row.verdict_file else "Evaluate"
        evaluate.blocked_reason = "The brief is empty: open it and write the description first" if row.brief_empty else None
        record.blocked_reason = None if row.verdict_file else "Evaluate it first"
        hint.show_argv(commands.argv_for(self.app.ctx, self._specs["evaluate"], {"input_file": str(row.brief)}))

    # -- opening ------------------------------------------------------------------------------------

    @on(Button.Pressed, "#cand-open-brief")
    def _open_brief(self) -> None:
        row = self.selected()
        if row is not None:
            open_path(row.brief)

    @on(Button.Pressed, "#cand-open-verdict")
    def _open_verdict(self) -> None:
        row = self.selected()
        if row is not None and row.verdict_file is not None:
            open_path(row.verdict_file)

    # -- evaluating and deciding --------------------------------------------------------------------------

    @on(RunButton.Pressed, "#cand-evaluate")
    def _evaluate(self) -> None:
        row = self.selected()
        if row is None or row.brief_empty:
            return
        argv = commands.argv_for(self.app.ctx, self._specs["evaluate"], {"input_file": str(row.brief)})
        warnings = [f"{row.slug} has already been evaluated. Running it again replaces the verdict at "
                    f"{row.verdict_file}."] if row.verdict_file else []
        try:
            guess = estimate.estimate_task(self.app.ctx, "s5", "evaluate", row.brief)
        except Exception:  # noqa: BLE001 - an estimate must never stop the operator from running
            guess = None
        self.app.begin_run(RunRequest(f"s5 evaluate {row.slug}", argv, estimate=guess, warnings=warnings))

    @on(Select.Changed, "#cand-decision-select")
    @on(Input.Changed, "#cand-decision-text")
    def _decision_edited(self) -> None:
        self.query_one("#cand-decision-error", Static).update("")

    @on(RunButton.Pressed, "#cand-record")
    def _record(self) -> None:
        row = self.selected()
        if row is None or row.verdict_file is None:
            return
        chosen = self.query_one("#cand-decision-select", Select).value
        decision = self.query_one("#cand-decision-text", Input).value.strip() or (chosen if isinstance(chosen, str) else "")
        if not decision:
            self.query_one("#cand-decision-error", Static).update("Choose a decision or write your own.")
            return
        argv = commands.argv_for(self.app.ctx, self._specs["record"], {"candidate_slug": row.slug, "decision": decision})
        request = RunRequest(f"candidate record-decision {row.slug}", argv)
        self._pending[id(request)] = self._clear_decision
        self.app.begin_run(request)

    def _clear_decision(self, result) -> None:
        if result.returncode == 0:
            self.query_one("#cand-decision-select", Select).clear()
            self.query_one("#cand-decision-text", Input).value = ""

    # -- a new candidate ------------------------------------------------------------------------------------

    @on(Input.Changed, "#cand-new-slug")
    def _slug_typed(self) -> None:
        self.query_one("#cand-new-error", Static).update("")

    @on(RunButton.Pressed, "#cand-new")
    def _new(self) -> None:
        slug = self.query_one("#cand-new-slug", Input).value.strip()
        problem = files.candidate_slug_problem(self.app.ctx, slug)
        if problem:
            self.query_one("#cand-new-error", Static).update(problem)
            return
        argv = commands.argv_for(self.app.ctx, self._specs["new"], {"candidate_slug": slug})
        request = RunRequest(f"candidate new {slug}", argv)
        self._pending[id(request)] = lambda result: self._brief_created(slug, result)
        self.app.begin_run(request)

    def _brief_created(self, slug: str, result) -> None:
        """After `candidate new`: make the empty brief file and open it for writing."""
        if result.returncode != 0:
            return
        path = files.create_brief(self.app.ctx, slug)
        self.query_one("#cand-new-slug", Input).value = ""
        self.refresh_state()
        open_path(path)
