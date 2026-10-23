"""Entry screen: every book (newest activity first) and a shortcut to every other screen."""
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, OptionList, Static
from textual.widgets.option_list import Option

from tui.screens.base import BaseScreen
from tui.screens.registry import ENTRIES
from tui.services import state


class HomeScreen(BaseScreen):
    BINDINGS = [Binding("n", "new_book", "New book"),
                *[Binding(e.key, f"open('{e.name}')", e.title) for e in ENTRIES]]

    def compose_body(self) -> ComposeResult:
        yield Static("Viable Imprint - editorial pipeline", id="welcome")
        yield Static("", id="books-empty", markup=False)
        yield DataTable(id="books", cursor_type="row", zebra_stripes=True)
        yield OptionList(*[Option(f"{e.key}  {e.title} - {e.description}", id=e.name) for e in ENTRIES], id="menu")

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("Book", "Tasks", "Failed", "Stale", "ISBN", "Last activity")
        self.refresh_books()

    def on_screen_resume(self) -> None:
        self.refresh_books()

    def run_finished(self, result) -> None:
        self.refresh_books()

    def refresh_books(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        books = state.list_books(self.app.ctx)
        for b in books:
            cells = [b.slug, f"{b.done}/{b.tasks_total}", str(b.failed) if b.failed else "-",
                     str(b.stale) if b.stale else "-", b.isbn or "not set", state.format_activity(b.last_activity)]
            if b.done_on:
                # A finished book goes after the ones in progress, with a green [x], the rest dimmed.
                cells = [Text.assemble((f"{state.DONE_MARK} ", "bold green"), (b.slug, "dim")),
                         *[Text(c, style="dim") for c in cells[1:5]], Text(f"done {b.done_on}", style="dim")]
            table.add_row(*cells, key=b.slug)
        empty = self.query_one("#books-empty", Static)
        empty.update("" if books else "No books yet. Press n to start one.")
        empty.display = not books
        table.display = bool(books)
        (table if books else self.query_one(OptionList)).focus()

    def action_new_book(self) -> None:
        from tui.screens.new_book import NewBookScreen
        self.app.push_screen(NewBookScreen())

    def action_open(self, name: str) -> None:
        self.app.open_screen(name)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.app.open_book(event.row_key.value)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.app.open_screen(event.option.id)
