"""System 3: the dashboard, sales ingest and the ISBN shortcut (ADR 023, Phase 4).
T4.1 is the dashboard; T4.2 adds sales ingest below it.
"""
from textual import on
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import DataTable, Static

from tui.screens.base import BaseScreen
from tui.services import dashboard_view


class PerformanceScreen(BaseScreen):
    def __init__(self):
        super().__init__()
        self.portfolio: dashboard_view.PortfolioView = dashboard_view.PortfolioView()
        self.book: dashboard_view.BookView = None

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(id="perf-scroll"):
            yield Static("", id="portfolio-title", markup=False)
            yield DataTable(id="portfolio", cursor_type="row", zebra_stripes=True)
            yield Static("", id="portfolio-totals", markup=False)
            yield Static("", id="portfolio-flags", markup=False)
            yield Static(dashboard_view.MARGIN_NOTE, id="margin-note", markup=False)
            yield Static("", id="book-title", markup=False)
            yield DataTable(id="book-tasks", cursor_type="row", zebra_stripes=True)
            yield Static("", id="book-summary", markup=False)

    def on_mount(self) -> None:
        self.query_one("#portfolio", DataTable).add_columns(
            "Book", "Tasks done", "API cost", "Compute time", "Revenue", "Margin (API)", "In-pipeline span")
        self.query_one("#book-tasks", DataTable).add_columns("Task", "Duration", "Provider/Model", "Usage", "Cost")
        self.refresh_dashboard()

    def on_screen_resume(self) -> None:
        self.refresh_dashboard()

    def run_finished(self, result) -> None:
        self.refresh_dashboard()

    def refresh_dashboard(self) -> None:
        table = self.query_one("#portfolio", DataTable)
        previous = table.cursor_row
        table.clear()
        self.portfolio = dashboard_view.portfolio_view(self.app.ctx)
        for r in self.portfolio.rows:
            table.add_row(r.slug, r.tasks, r.cost, r.compute, r.revenue, r.margin, r.span, key=r.slug)
        if not self.portfolio.rows:
            self.query_one("#portfolio-title", Static).update(f"No books found under {self.app.ctx.data_dir('books_dir')}/")
            self.query_one("#portfolio-totals", Static).update("")
            self.query_one("#portfolio-flags", Static).update("")
            self._show_book(None)
            return
        self.query_one("#portfolio-title", Static).update(self.portfolio.heading)
        self.query_one("#portfolio-totals", Static).update(self.portfolio.totals)
        self.query_one("#portfolio-flags", Static).update("\n".join(self.portfolio.flags))
        table.move_cursor(row=min(previous, table.row_count - 1))
        self._show_book(self.selected_slug())

    def selected_slug(self):
        table = self.query_one("#portfolio", DataTable)
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    @on(DataTable.RowHighlighted, "#portfolio")
    def _book_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_book(self.selected_slug())

    def _show_book(self, slug) -> None:
        table = self.query_one("#book-tasks", DataTable)
        table.clear()
        if slug is None:
            self.book = None
            self.query_one("#book-title", Static).update("")
            self.query_one("#book-summary", Static).update("")
            return
        self.book = dashboard_view.book_view(self.app.ctx, slug)
        for t in self.book.tasks:
            table.add_row(t.label, t.duration, t.provider_model, t.usage, t.cost)
        self.query_one("#book-title", Static).update(f"Book: {slug}")
        lines = [self.book.total, self.book.span]
        if self.book.revenue:
            lines += [self.book.revenue, self.book.margin]
        lines += self.book.flags
        self.query_one("#book-summary", Static).update("\n".join(lines))
