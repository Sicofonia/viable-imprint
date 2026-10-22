"""System 3: the dashboard and sales ingest (ADR 023, Phase 4).

T4.1 is the portfolio and per-book dashboard, worded exactly as `s3 dashboard` words it.
T4.2 is the sales ingest panel: pick a book (one without an ISBN is sent to set it first),
pick the royalty CSV, let the format be detected or force one, run `s3 sales-ingest`,
read the receipt, and see the dashboard refresh.
"""
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Input, Select, Static

import providers.sales as sales_formats
from tui.screens.base import BaseScreen
from tui.screens.file_picker import FilePickerScreen
from tui.services import commands, dashboard_view, state
from tui.services.command_model import build_model
from tui.services.output import ingest_receipt
from tui.services.session import RunRequest
from tui.widgets.command_hint import CommandHint
from tui.widgets.run_button import RunButton

AUTO = "auto"
FORMAT_OPTIONS = [("Auto-detect", AUTO), *[(f.name, f.name) for f in sales_formats.FORMATS]]


class PerformanceScreen(BaseScreen):
    def __init__(self):
        super().__init__()
        self.portfolio: dashboard_view.PortfolioView = dashboard_view.PortfolioView()
        self.book: dashboard_view.BookView = None
        self._pending: RunRequest = None      # the ingest this screen started

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
            yield Static("Sales ingest", id="sales-title", markup=False)
            yield Static("A royalty or sales CSV you downloaded by hand from a platform, matched to a book by its ISBN.",
                         id="sales-help", markup=False)
            yield Select([], id="ingest-book", prompt="Choose a book")
            with Horizontal(id="ingest-csv-row"):
                yield Input(id="ingest-csv", placeholder="path to the downloaded CSV")
                yield Button("Browse", id="browse-csv")
            yield Select(FORMAT_OPTIONS, id="ingest-format", allow_blank=False, value=AUTO)
            yield CommandHint(id="ingest-hint")
            with Horizontal(id="ingest-actions"):
                yield RunButton("Ingest sales CSV", id="run-ingest")
                yield Button("Set ISBN first", id="set-isbn-first")
            yield Static("", id="ingest-receipt", markup=False)

    def on_mount(self) -> None:
        self.query_one("#portfolio", DataTable).add_columns(
            "Book", "Tasks done", "API cost", "Compute time", "Revenue", "Margin (API)", "In-pipeline span")
        self.query_one("#book-tasks", DataTable).add_columns("Task", "Duration", "Provider/Model", "Usage", "Cost")
        self.query_one("#set-isbn-first").display = False
        self.refresh_dashboard()

    def on_screen_resume(self) -> None:
        self.refresh_dashboard()

    def run_finished(self, result) -> None:
        if self._pending is not None and self.app.session is not None and self.app.session.request is self._pending:
            receipt = ingest_receipt(self.app.session.lines)
            self.query_one("#ingest-receipt", Static).update(receipt.sentence() if receipt else "")
            self._pending = None
        self.refresh_dashboard()

    # -- the dashboard ----------------------------------------------------------------

    def refresh_dashboard(self) -> None:
        table = self.query_one("#portfolio", DataTable)
        previous = table.cursor_row
        table.clear()
        self.portfolio = dashboard_view.portfolio_view(self.app.ctx)
        for r in self.portfolio.rows:
            table.add_row(r.slug, r.tasks, r.cost, r.compute, r.revenue, r.margin, r.span, key=r.slug)
        self._refresh_book_choices()
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

    # -- sales ingest ---------------------------------------------------------------------

    def _refresh_book_choices(self) -> None:
        select = self.query_one("#ingest-book", Select)
        books = state.list_books(self.app.ctx)
        previous = select.value
        select.set_options([(f"{b.slug}" if b.isbn else f"{b.slug} (no ISBN)", b.slug) for b in sorted(books, key=lambda b: b.slug)])
        slugs = [b.slug for b in books]
        # Keep the operator's choice; until they make one, follow the book highlighted above.
        choice = previous if previous in slugs else self.selected_slug()
        if choice in slugs:
            select.value = choice
        self._update_ingest()

    def _chosen_book(self):
        value = self.query_one("#ingest-book", Select).value
        return value if isinstance(value, str) else None

    def _ingest_values(self) -> dict:
        chosen = self.query_one("#ingest-format", Select).value
        csv = self.query_one("#ingest-csv", Input).value.strip().strip('"')
        return {"book_slug": self._chosen_book(), "csv_file": csv or None, "format_": None if chosen == AUTO else chosen}

    def _ingest_blocker(self, values: dict):
        slug = values["book_slug"]
        if not slug:
            return "Choose a book"
        if not state.book_row(self.app.ctx.data_dir("books_dir") / slug).isbn:
            return "Set this book's ISBN first"
        if not values["csv_file"]:
            return "Choose the CSV file"
        if not Path(values["csv_file"]).is_file():
            return "That file does not exist"
        return None

    def _update_ingest(self) -> None:
        values = self._ingest_values()
        blocker = self._ingest_blocker(values)
        self.query_one("#run-ingest", RunButton).blocked_reason = blocker
        self.query_one("#set-isbn-first").display = blocker == "Set this book's ISBN first"
        hint = self.query_one("#ingest-hint", CommandHint)
        if values["book_slug"] and values["csv_file"]:
            hint.show_argv(commands.argv_for(self.app.ctx, self._ingest_spec(), values))
        else:
            hint.clear()

    def _ingest_spec(self):
        return next(s for s in build_model(self.app.root) if s.path == ["s3", "sales-ingest"])

    @on(Select.Changed, "#ingest-book")
    @on(Select.Changed, "#ingest-format")
    @on(Input.Changed, "#ingest-csv")
    def _ingest_inputs_changed(self) -> None:
        self._update_ingest()

    @on(Button.Pressed, "#browse-csv")
    def _browse_csv(self) -> None:
        downloads = Path.home() / "Downloads"
        start = downloads if downloads.is_dir() else Path.home()

        def chosen(path):
            if path:
                self.query_one("#ingest-csv", Input).value = path

        self.app.push_screen(FilePickerScreen(start, title="Choose the royalty or sales CSV"), chosen)

    @on(Button.Pressed, "#set-isbn-first")
    def _set_isbn_first(self) -> None:
        slug = self._chosen_book()
        if slug:
            self.app.open_book(slug)

    @on(RunButton.Pressed, "#run-ingest")
    def _run_ingest(self) -> None:
        values = self._ingest_values()
        if self._ingest_blocker(values):
            return
        argv = commands.argv_for(self.app.ctx, self._ingest_spec(), values)
        self._pending = RunRequest(f"s3 sales-ingest {values['book_slug']}", argv)
        self.query_one("#ingest-receipt", Static).update("")
        self.app.begin_run(self._pending)
