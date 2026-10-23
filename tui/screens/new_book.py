"""New book: a name and the manuscript. Runs `init` through the CLI, then copies
the chosen .txt into `s1b/source/` (the only thing the UI writes itself) and opens
the book (ADR 023, T2.1)."""
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from tui.screens.file_picker import FilePickerScreen
from tui.services import commands, files
from tui.services.command_model import build_model
from tui.services.session import RunRequest
from tui.widgets.command_hint import CommandHint


def finish_new_book(app, slug: str, source: str, result) -> None:
    """After `init` has run: copy the manuscript in and open the book."""
    if result.returncode != 0:
        app.notify(f"init {slug} did not complete; the manuscript was not copied.", severity="error")
        return
    try:
        files.copy_source(app.ctx, slug, source)
    except (OSError, FileExistsError) as exc:
        app.notify(f"The book {slug} was created but the manuscript was not copied: {exc}", severity="error")
    app.open_book(slug)


class NewBookScreen(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="newbook-dialog"):
            yield Label("New book", id="newbook-title")
            yield Label("Name (lowercase letters, digits, hyphens)")
            yield Input(id="nb-slug", placeholder="life-as-explorer")
            yield Label("Manuscript (.txt)")
            with Horizontal(id="nb-source-row"):
                yield Input(id="nb-source", placeholder="path to the source text file")
                yield Button("Browse", id="nb-browse")
            yield Static("", id="nb-error", markup=False)
            yield CommandHint(id="nb-hint")
            with Horizontal(id="nb-buttons"):
                yield Button("Create", id="nb-create", variant="primary")
                yield Button("Cancel", id="nb-cancel")

    def on_mount(self) -> None:
        self.query_one("#nb-slug", Input).focus()
        self._update_hint()

    def _init_spec(self):
        return next(s for s in build_model(self.app.root) if s.path == ["init"])

    def _update_hint(self) -> None:
        slug = self.query_one("#nb-slug", Input).value.strip()
        hint = self.query_one(CommandHint)
        if slug:
            hint.show_argv(commands.argv_for(self.app.ctx, self._init_spec(), {"book_slug": slug}))
        else:
            hint.clear()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.query_one("#nb-error", Static).update("")
        self._update_hint()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button.id
        if button == "nb-cancel":
            self.dismiss(None)
        elif button == "nb-browse":
            self.app.push_screen(FilePickerScreen(self.app.ctx.repo_root, title="Choose the manuscript .txt"),
                                 self._picked)
        elif button == "nb-create":
            self._create()

    def _picked(self, path) -> None:
        if path:
            self.query_one("#nb-source", Input).value = path

    def _create(self) -> None:
        slug = self.query_one("#nb-slug", Input).value.strip()
        source = self.query_one("#nb-source", Input).value.strip().strip('"')
        problem = files.slug_problem(self.app.ctx, slug) or files.source_problem(source)
        if problem:
            self.query_one("#nb-error", Static).update(problem)
            return
        app = self.app
        argv = commands.argv_for(app.ctx, self._init_spec(), {"book_slug": slug})
        request = RunRequest(f"init {slug}", argv, on_done=lambda result: finish_new_book(app, slug, source, result))
        self.dismiss(None)
        app.begin_run(request)

    def action_cancel(self) -> None:
        self.dismiss(None)
