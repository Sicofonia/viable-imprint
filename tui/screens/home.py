"""Entry screen. Books and the shortcuts to every other screen arrive in later tasks."""
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header, Static


class HomeScreen(Screen):
    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Viable Imprint", id="welcome")
        yield Footer()
