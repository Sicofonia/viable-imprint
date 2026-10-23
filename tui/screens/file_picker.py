"""A small file picker: type or paste a folder, then choose a file in the tree."""
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DirectoryTree, Input, Label


class FilePickerScreen(ModalScreen):
    """Dismisses with the chosen file's path (a string), or None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, start: Path, *, title: str = "Choose a file"):
        super().__init__()
        self.start = Path(start)
        self.title_text = title

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog"):
            yield Label(self.title_text, id="picker-title")
            yield Input(str(self.start), id="picker-folder", placeholder="Folder to browse")
            yield DirectoryTree(self.start, id="picker-tree")
            with Horizontal(id="picker-buttons"):
                yield Button("Cancel", id="picker-cancel")

    def on_mount(self) -> None:
        self.query_one(DirectoryTree).focus()  # arrow keys and enter choose a file; the folder box is a click away

    def on_input_submitted(self, event: Input.Submitted) -> None:
        folder = Path(event.value.strip().strip('"'))
        if folder.is_dir():
            tree = self.query_one(DirectoryTree)
            tree.path = folder
            tree.reload()

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self.dismiss(str(event.path))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
