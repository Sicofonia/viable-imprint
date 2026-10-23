"""The `?` panel: key bindings and the one-run-at-a-time rule."""
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

NOTES = (
    "One command runs at a time. The run log keeps going if you hide it (escape); "
    "ctrl+r brings it back, and buttons that start a run are disabled until it ends.\n\n"
    "Do not run pipeline.py in another terminal while the UI is running something: the "
    "run-state ledger is not locked, so two writers can lose an update. The UI cannot "
    "prevent that.\n\n"
    "Every action shows its command-line equivalent before it runs. Visual outputs open "
    "in your operating system's default application."
)


def binding_lines(screens: list, app) -> list:
    """(key, description) for the app's bindings and those of each screen in `screens`."""
    seen, lines = set(), []
    for owner in [*screens, app]:
        for binding in owner.BINDINGS:
            if isinstance(binding, tuple):
                key, _, description = binding
                display = key
            else:
                key, description = binding.key, binding.description
                display = binding.key_display or key
            if description and (display, description) not in seen:
                seen.add((display, description))
                lines.append((display, description))
    return lines


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape", "close", "Close"), Binding("question_mark", "close", "Close", show=False)]

    def __init__(self, lines: list):
        super().__init__()
        self._lines = lines

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            yield Label("Keys", id="help-title")
            with VerticalScroll():
                yield Static("\n".join(f"{key:<12} {description}" for key, description in self._lines), markup=False,
                             id="help-keys")
                yield Static(NOTES, markup=False, id="help-notes")
            yield Button("Close", id="help-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()

    def action_close(self) -> None:
        self.dismiss()
