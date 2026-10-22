"""One book: the state of every task, run them, open their outputs (ADR 023,
Phase 2). T2.2 is the status view; T2.3 to T2.5 add the actions beside it.
"""
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DataTable, Static

from tui.screens.base import BaseScreen
from tui.services import state
from tui.services.command_model import group_helps

MARKS = {"done": "[x]", "stale": "[~]", "ready": "[ ]", "blocked": "[ ]", "failed": "[!]"}


class BookScreen(BaseScreen):
    BINDINGS = BaseScreen.BINDINGS + [Binding("f5", "refresh", "Refresh")]

    def __init__(self, slug: str):
        super().__init__()
        self.slug = slug
        self.tasks: dict = {}          # row key -> state.TaskRow
        self.system_labels: dict = {}

    @property
    def root(self):
        return self.app.ctx.data_dir("books_dir") / self.slug

    def compose_body(self) -> ComposeResult:
        yield Static("", id="book-title", markup=False)
        with Horizontal(id="book-main"):
            yield DataTable(id="tasks", cursor_type="row")
            with VerticalScroll(id="task-panel"):
                yield Static("", id="task-detail", markup=False)

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
        self._show_selected()

    def selected(self):
        """The `state.TaskRow` under the cursor, or None on a system heading."""
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        return self.tasks.get(key)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_selected()

    def _show_selected(self) -> None:
        row = self.selected()
        panel = self.query_one("#task-detail", Static)
        if row is None:
            panel.update("Select a task.")
            return
        lines = [f"{row.system} {row.name}", f"State: {row.status}", row.detail]
        if row.output:
            lines.append(f"Output: {row.output}")
        panel.update("\n".join(lines))
