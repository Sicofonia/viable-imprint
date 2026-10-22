"""One book: status of every task, running them, outputs. Built up over T2.2-T2.5."""
from textual.app import ComposeResult
from textual.widgets import Static

from tui.screens.base import BaseScreen


class BookScreen(BaseScreen):
    def __init__(self, slug: str):
        super().__init__()
        self.slug = slug

    def compose_body(self) -> ComposeResult:
        yield Static(f"Book: {self.slug}", id="book-title")
