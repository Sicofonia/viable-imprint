"""One book: the state of every task, run them, open their outputs (ADR 023,
Phase 2). T2.2 is the status view; T2.3 runs a single task; T2.4 and T2.5 add the
pipeline run and the outputs beside it.
"""
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DataTable, Static

from tui.screens.base import BaseScreen
from tui.services import commands, estimate, state
from tui.services.command_model import CommandSpec, build_model, group_helps
from tui.services.session import RunRequest
from tui.widgets.command_hint import CommandHint
from tui.widgets.param_form import ParamForm
from tui.widgets.run_button import RunButton

MARKS = {"done": "[x]", "stale": "[~]", "ready": "[ ]", "blocked": "[ ]", "failed": "[!]"}


class BookScreen(BaseScreen):
    BINDINGS = BaseScreen.BINDINGS + [Binding("f5", "refresh", "Refresh")]

    def __init__(self, slug: str):
        super().__init__()
        self.slug = slug
        self.tasks: dict = {}          # row key -> state.TaskRow
        self.system_labels: dict = {}
        self._options_for = None       # which task the options form is currently built for

    @property
    def root(self):
        return self.app.ctx.data_dir("books_dir") / self.slug

    def compose_body(self) -> ComposeResult:
        yield Static("", id="book-title", markup=False)
        with Horizontal(id="book-main"):
            yield DataTable(id="tasks", cursor_type="row")
            with VerticalScroll(id="task-panel"):
                yield Static("", id="task-detail", markup=False)
                yield ParamForm(id="task-options")
                yield CommandHint(id="task-hint")
                with Horizontal(id="task-actions"):
                    yield RunButton("Run task", id="run-task")

    def on_mount(self) -> None:
        self.system_labels = {path[0]: label for path, label in group_helps(self.app.root).items() if len(path) == 1}
        self.query_one(DataTable).add_columns("Task", "State", "Detail")
        self.refresh_state()
        self.query_one(DataTable).focus()

    def on_screen_resume(self) -> None:
        self.refresh_state()

    def run_finished(self, result) -> None:
        self.refresh_state()

    def action_refresh(self) -> None:
        self.refresh_state()

    # -- reading state ------------------------------------------------------------

    def refresh_state(self) -> None:
        table = self.query_one(DataTable)
        previous = table.cursor_row
        table.clear()
        self.tasks = {}
        current_system = None
        for row in state.book_tasks(self.root):
            if row.system != current_system:
                current_system = row.system
                table.add_row(self.system_labels.get(row.system, row.system), "", "", key=f"hdr:{row.system}")
            self.tasks[row.key] = row
            table.add_row(f"{MARKS[row.status]} {row.name}", row.status, row.detail, key=row.key)
        if table.row_count:
            table.move_cursor(row=min(previous, table.row_count - 1))
        info = state.book_row(self.root)
        self.query_one("#book-title", Static).update(
            f"{self.slug}   ISBN: {info.isbn or 'not set'}   {info.done}/{info.tasks_total} done"
            f"{f', {info.failed} failed' if info.failed else ''}{f', {info.stale} stale' if info.stale else ''}"
            f"   {self.root}")
        self.call_later(self._show_selected)

    def selected(self):
        """The `state.TaskRow` under the cursor, or None on a system heading."""
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return self.tasks.get(key)

    @on(DataTable.RowHighlighted, "#tasks")
    async def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        await self._show_selected()

    # -- the task panel -------------------------------------------------------------

    def _task_spec(self, row) -> CommandSpec:
        return next(s for s in build_model(self.app.root) if s.path == [row.system, row.name])

    def _options_spec(self, row) -> CommandSpec:
        """The task's command without its input file: that is resolved, not typed."""
        spec = self._task_spec(row)
        return CommandSpec(spec.path, spec.help, [p for p in spec.params if p.kind != "argument"])

    async def _show_selected(self) -> None:
        row = self.selected()
        panel = self.query_one("#task-detail", Static)
        options = self.query_one("#task-options", ParamForm)
        button = self.query_one("#run-task", RunButton)
        if row is None:
            panel.update("Select a task.")
            self._options_for = None
            await options.set_spec(CommandSpec([], "", []))
            self.query_one("#task-hint", CommandHint).clear()
            button.display = False
            return
        lines = [f"{row.system} {row.name}", f"State: {row.status}", row.detail]
        if row.output:
            lines.append(f"Output: {row.output}")
        panel.update("\n".join(lines))
        if self._options_for != row.key:
            self._options_for = row.key
            await options.set_spec(self._options_spec(row))
        button.display = True
        button.label = "Run again" if row.status == "done" else "Retry" if row.status == "failed" else "Run task"
        button.blocked_reason = None if row.runnable else row.detail
        self._update_hint(row)

    def _argv_for(self, row):
        input_path, reason = state.task_input(self.root, row)
        if input_path is None:
            raise commands.MissingParameter(reason)
        values = {"input_file": str(input_path), **self.query_one("#task-options", ParamForm).values()}
        return commands.argv_for(self.app.ctx, self._task_spec(row), values), input_path, values

    def _update_hint(self, row) -> None:
        hint = self.query_one("#task-hint", CommandHint)
        try:
            hint.show_argv(self._argv_for(row)[0])
        except (commands.MissingParameter, commands.InvalidValue) as exc:
            hint.show_error(f"Cannot run yet: {exc}")

    @on(ParamForm.Changed)
    def _options_changed(self) -> None:
        row = self.selected()
        if row is not None:
            self._update_hint(row)

    # -- running one task -------------------------------------------------------------

    @on(RunButton.Pressed, "#run-task")
    def _run_task(self) -> None:
        row = self.selected()
        if row is None or not row.runnable:
            return
        try:
            argv, input_path, values = self._argv_for(row)
        except (commands.MissingParameter, commands.InvalidValue) as exc:
            self.query_one("#task-hint", CommandHint).show_error(f"Cannot run yet: {exc}")
            return
        warnings = []
        if row.status == "done":
            warnings.append(f"{row.system} {row.name} has already run. Running it again overwrites {row.output}.")
            downstream = state.stale_consequences(self.root, row)
            if downstream:
                warnings.append("These finished tasks will be marked stale: "
                                + ", ".join(k.split(".", 1)[1] for k in downstream) + ".")
        try:
            guess = estimate.estimate_task(self.app.ctx, row.system, row.name, input_path,
                                           document=bool(values.get("document")))
        except Exception:  # noqa: BLE001 - an estimate must never stop the operator from running
            guess = None
        self.app.begin_run(RunRequest(f"{row.system} {row.name}", argv, estimate=guess, warnings=warnings))
