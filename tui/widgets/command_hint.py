"""The "Equivalent command" line shown on every action screen (ADR 023, Decision 4).

The UI teaches the CLI instead of hiding it: what is shown here is exactly what
`services/commands.py` builds and the runner executes.
"""
from textual.widgets import Static

from tui.services import commands


class CommandHint(Static):
    DEFAULT_CSS = """
    CommandHint {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }
    CommandHint.error {
        color: $warning;
    }
    """

    def __init__(self, **kwargs):
        # markup=False: command lines and error messages contain square brackets.
        super().__init__("", markup=False, **kwargs)
        self.text = ""

    def show_argv(self, argv: list) -> None:
        self.remove_class("error")
        self._put_text(f"Equivalent command:\n  {commands.display(argv)}")

    def show_error(self, message: str) -> None:
        self.add_class("error")
        self._put_text(message)

    def clear(self) -> None:
        self.remove_class("error")
        self._put_text("")

    def _put_text(self, text: str) -> None:
        self.text = text
        self.update(text)
