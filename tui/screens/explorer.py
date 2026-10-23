"""The generic command explorer (ADR 023, Decision 3): a filterable tree of every
command in the Click tree, a form for the selected one, its command-line
equivalent and a Run button. Nothing here knows any particular command, so a
command added to any system appears with a working form and no UI change. This is
what replaces the hand-maintained cheat sheet.
"""
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, Static, Tree
from rich.text import Text

from tui.screens.base import BaseScreen
from tui.services import commands, estimate
from tui.services.command_model import CommandSpec, build_model, group_helps
from tui.services.command_notes import extra_confirmation, not_run_directly
from tui.services.session import RunRequest
from tui.widgets.command_hint import CommandHint
from tui.widgets.param_form import ParamForm
from tui.widgets.run_button import RunButton


def first_line(text: str, limit: int = 60) -> str:
    line = (text or "").strip().split("\n")[0].strip()
    return line if len(line) <= limit else line[: limit - 1] + "..."


def matches(spec: CommandSpec, needle: str) -> bool:
    haystack = " ".join([spec.name, spec.help, *(f"{p.name} {' '.join(p.opts)} {p.help or ''}" for p in spec.params)])
    return needle.lower() in haystack.lower()


class ExplorerScreen(BaseScreen):
    BINDINGS = BaseScreen.BINDINGS + [Binding("slash", "focus_filter", "Filter", key_display="/")]

    def __init__(self):
        super().__init__()
        self.specs: list = []
        self.group_labels: dict = {}
        self.spec: CommandSpec = None

    def compose_body(self) -> ComposeResult:
        with Horizontal(id="explorer"):
            with Vertical(id="explorer-left"):
                yield Input(placeholder="Filter commands (e.g. cover, isbn)", id="filter")
                yield Tree("Commands", id="commands")
            with VerticalScroll(id="explorer-right"):
                yield Static("Select a command on the left.", id="detail-title", markup=False)
                yield Static("", id="detail-help", markup=False)
                yield Static("", id="detail-warning", markup=False)
                yield ParamForm(id="form")
                yield CommandHint(id="hint")
                yield RunButton("Run", id="run")

    def on_mount(self) -> None:
        self.specs = build_model(self.app.root)
        self.group_labels = group_helps(self.app.root)
        self.query_one(ParamForm).browse_start = self.app.ctx.repo_root
        self.query_one("#run").display = False
        self._populate("")
        self.query_one(Tree).focus()

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    # -- the tree -------------------------------------------------------------

    def _populate(self, needle: str) -> None:
        tree = self.query_one(Tree)
        tree.clear()
        tree.root.expand()
        nodes = {(): tree.root}
        for spec in self.specs:
            if needle and not matches(spec, needle):
                continue
            for depth in range(1, len(spec.path)):
                key = tuple(spec.path[:depth])
                if key not in nodes:
                    label = f"{key[-1]}  {first_line(self.group_labels.get(key, ''))}".strip()
                    nodes[key] = nodes[key[:-1]].add(Text(label), expand=True)
            leaf_label = f"{spec.path[-1]}  {first_line(spec.help, 44)}".strip()
            nodes[tuple(spec.path[:-1])].add_leaf(Text(leaf_label), data=spec)

    def visible_paths(self) -> list:
        """The command paths currently listed in the tree, in tree order."""
        found = []

        def walk(node):
            for child in node.children:
                if isinstance(child.data, CommandSpec):
                    found.append(child.data.path)
                walk(child)

        walk(self.query_one(Tree).root)
        return found

    def select_path(self, path: list) -> bool:
        """Move the tree cursor onto a command. False if it is filtered out."""
        tree = self.query_one(Tree)

        def find(node):
            for child in node.children:
                if isinstance(child.data, CommandSpec) and child.data.path == path:
                    return child
                hit = find(child)
                if hit is not None:
                    return hit
            return None

        node = find(tree.root)
        if node is None:
            return False
        tree.move_cursor(node)
        return True

    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        self._populate(event.value.strip())

    @on(Tree.NodeHighlighted)
    async def _command_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if isinstance(event.node.data, CommandSpec):
            await self._show(event.node.data)

    # -- the detail panel -----------------------------------------------------

    async def _show(self, spec: CommandSpec) -> None:
        self.spec = spec
        self.query_one("#detail-title", Static).update(spec.name)
        self.query_one("#detail-help", Static).update(spec.help or "(this command has no description)")
        warning = not_run_directly(spec)
        self.query_one("#detail-warning", Static).update(f"! {warning}" if warning else "")
        run = self.query_one("#run", RunButton)
        run.display = warning is None
        await self.query_one(ParamForm).set_spec(spec)
        self._update_hint()

    def _values(self) -> dict:
        return self.query_one(ParamForm).values()

    def _update_hint(self) -> None:
        hint = self.query_one(CommandHint)
        if self.spec is None:
            hint.clear()
            return
        try:
            hint.show_argv(commands.argv_for(self.app.ctx, self.spec, self._values()))
        except (commands.MissingParameter, commands.InvalidValue) as exc:
            hint.show_error(f"Not ready yet: {exc}")

    @on(ParamForm.Changed)
    def _form_changed(self) -> None:
        self._update_hint()

    @on(RunButton.Pressed, "#run")
    def _run(self) -> None:
        if self.spec is None:
            return
        values = self._values()
        try:
            argv = commands.argv_for(self.app.ctx, self.spec, values)
        except (commands.MissingParameter, commands.InvalidValue) as exc:
            self.query_one(CommandHint).show_error(f"Cannot run yet: {exc}")
            return
        try:
            guess = estimate.estimate_command(self.app.ctx, self.spec.path, values)
        except Exception:  # noqa: BLE001 - an estimate must never stop the operator from running
            guess = None
        extra = extra_confirmation(self.spec, values)
        self.app.begin_run(RunRequest(self.spec.name, argv, estimate=guess,
                                      warnings=[extra] if extra else [], confirm=bool(extra)))
