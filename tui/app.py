"""PipelineApp: screen routing, key bindings, command palette (ADR 023)."""
import click
from textual.app import App
from textual.binding import Binding

from tui.screens.home import HomeScreen
from tui.services.context import Context


class PipelineApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "Viable Imprint"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, ctx: Context, root: click.Group):
        super().__init__()
        self.ctx = ctx
        self.root = root

    def on_mount(self) -> None:
        self.push_screen(HomeScreen())


def run_app(root: click.Group, config_path: str) -> None:
    """Called by `pipeline.py ui` with its own Click group and `--config` value."""
    PipelineApp(Context.load(config_path), root).run()
