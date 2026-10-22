"""Health (ADR 023, T8.1): a read-only report of what would make a run fail, with a link to
the file or screen that fixes each item. Nothing here changes anything."""
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Button, DataTable, Static

from tui.screens.base import BaseScreen
from tui.services import health
from tui.services.files import open_path


class HealthScreen(BaseScreen):
    BINDINGS = BaseScreen.BINDINGS + [Binding("f5", "refresh", "Re-check")]

    def __init__(self):
        super().__init__()
        self.checks: list = []
        # Tests point these at fixtures; None means the real environment.
        self.environ = None
        self.policy_files = None

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(id="health-scroll"):
            yield Static("", id="health-summary", markup=False)
            yield DataTable(id="health-checks", cursor_type="row", zebra_stripes=True)
            yield Static("", id="health-detail", markup=False)
            yield Button("Open the fix", id="health-fix", disabled=True)

    def on_mount(self) -> None:
        self.query_one("#health-checks", DataTable).add_columns("Area", "Check", "Status", "Detail")
        self.refresh_state()

    def on_screen_resume(self) -> None:
        self.refresh_state()

    def run_finished(self, result) -> None:
        self.refresh_state()

    def action_refresh(self) -> None:
        self.refresh_state()

    def refresh_state(self) -> None:
        table = self.query_one("#health-checks", DataTable)
        previous = table.cursor_row
        table.clear()
        self.checks = health.run_checks(self.app.ctx, environ=self.environ, policy_files=self.policy_files)
        for i, check in enumerate(self.checks):
            table.add_row(check.area, check.label, check.mark, check.detail, key=str(i))
        if table.row_count:
            table.move_cursor(row=min(previous, table.row_count - 1))
        self.query_one("#health-summary", Static).update(health.summary(self.checks))
        self._show_selected()

    def selected(self):
        table = self.query_one("#health-checks", DataTable)
        if not table.row_count:
            return None
        return self.checks[int(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)]

    @on(DataTable.RowHighlighted, "#health-checks")
    def _cursor_moved(self) -> None:
        self._show_selected()

    def _show_selected(self) -> None:
        check = self.selected()
        fix = self.query_one("#health-fix", Button)
        if check is None:
            self.query_one("#health-detail", Static).update("")
            fix.disabled = True
            return
        self.query_one("#health-detail", Static).update(f"{check.area}: {check.label}\n{check.detail}")
        fix.disabled = check.fix is None
        fix.label = {"file": "Open the file", "screen": "Go to the screen"}.get(check.fix[0] if check.fix else None, "Open the fix")

    @on(Button.Pressed, "#health-fix")
    def _open_fix(self) -> None:
        check = self.selected()
        if check is None or check.fix is None:
            return
        kind, target = check.fix
        if kind == "file":
            open_path(target)
        else:
            self.app.open_screen(target)
