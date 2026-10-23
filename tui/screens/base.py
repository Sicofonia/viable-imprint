"""Common frame for every screen: header, footer, `escape` back to the home screen."""
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, Static


class BaseScreen(Screen):
    BINDINGS = [Binding("escape", "go_home", "Home")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield from self.compose_body()
        yield Footer()

    def compose_body(self) -> ComposeResult:
        return iter(())

    def action_go_home(self) -> None:
        self.app.go_home()

    def run_finished(self, result) -> None:
        """Called by the app when a run ends, so the screen can re-read the state the run changed."""


class PlaceholderScreen(BaseScreen):
    """Stands in for a screen whose task has not been built yet."""
    heading = ""
    task = ""

    def compose_body(self) -> ComposeResult:
        yield Static(f"{self.heading}\n\nThis screen is built in task {self.task} of ADR 023.", id="placeholder")
