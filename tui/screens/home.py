"""Entry screen: a shortcut to every other screen. The books list arrives with T2.1."""
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from tui.screens.base import BaseScreen
from tui.screens.registry import ENTRIES


class HomeScreen(BaseScreen):
    BINDINGS = [Binding(e.key, f"open('{e.name}')", e.title) for e in ENTRIES]

    def compose_body(self) -> ComposeResult:
        yield Static("Viable Imprint - editorial pipeline", id="welcome")
        yield OptionList(*[Option(f"{e.key}  {e.title} - {e.description}", id=e.name) for e in ENTRIES], id="menu")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def action_open(self, name: str) -> None:
        self.app.open_screen(name)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.app.open_screen(event.option.id)
