"""System 1C print prep: check a PDF's page boxes, set the interior's TrimBox, set the
cover's TrimBox and BleedBox (ADR 023, T3.1; ADR 019).

Three forms generated from the `s1c` commands (so they cannot drift from the CLI), with
a file picker for the PDF, millimetre defaults, the PDF page size the cover export must
have shown while the numbers are typed, and an output name that defaults to
`<input>_print.pdf`. After a set-* command succeeds, "Check the result" opens the Check
form pre-filled with the file it wrote. Nothing here touches the run-state ledger.
"""
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Static, TabbedContent, TabPane

from tui.screens.base import BaseScreen
from tui.services import commands, printprep
from tui.services.command_model import build_model
from tui.services.session import RunRequest
from tui.widgets.command_hint import CommandHint
from tui.widgets.param_form import ParamForm
from tui.widgets.run_button import RunButton

# tab -> (command path, tab title, run button label)
TABS = {
    "check": (["s1c", "check"], "Check a PDF", "Check"),
    "interior": (["s1c", "set-interior"], "Set interior", "Set interior TrimBox"),
    "cover": (["s1c", "set-cover"], "Set cover", "Set cover boxes"),
}


class PrintPrepScreen(BaseScreen):
    def __init__(self):
        super().__init__()
        self.specs: dict = {}
        self._following = {"interior": True, "cover": True}   # output name still follows the input name
        self._produced: dict = {}                              # tab -> file its last successful run wrote
        self._pending: tuple = None                            # (request, tab, values) of the run we started

    def compose_body(self) -> ComposeResult:
        yield Static(printprep.WORKFLOW_NOTE, id="pp-note", markup=False)
        with TabbedContent(initial="tab-check", id="pp-tabs"):
            for tab, (_path, title, label) in TABS.items():
                with TabPane(title, id=f"tab-{tab}"):
                    with VerticalScroll():
                        yield ParamForm(id=f"form-{tab}")
                        if tab != "check":
                            yield Static("", id=f"size-{tab}", markup=False, classes="size")
                        yield CommandHint(id=f"hint-{tab}")
                        with Horizontal(classes="pp-actions"):
                            yield RunButton(label, id=f"run-{tab}")
                            if tab != "check":
                                yield Button("Check the result", id=f"again-{tab}", disabled=True)

    async def on_mount(self) -> None:
        model = {tuple(s.path): s for s in build_model(self.app.root)}
        for tab, (path, _title, _label) in TABS.items():
            self.specs[tab] = model[tuple(path)]
            form = self.query_one(f"#form-{tab}", ParamForm)
            form.browse_start = Path.home()
            await form.set_spec(self.specs[tab])
            self._refresh(tab)

    # -- keeping the preview, hint and output name current ----------------------------

    def _form(self, tab: str) -> ParamForm:
        return self.query_one(f"#form-{tab}", ParamForm)

    def _problem(self, tab: str, values: dict):
        output = values.get("output_file")
        if tab != "check" and output and values.get("input_file"):
            if Path(output).resolve() == Path(values["input_file"]).resolve():
                return "The output must differ from the input: it would overwrite the exported PDF."
        return None

    def _refresh(self, tab: str) -> None:
        values = self._form(tab).values()
        hint = self.query_one(f"#hint-{tab}", CommandHint)
        problem = self._problem(tab, values)
        try:
            argv = commands.argv_for(self.app.ctx, self.specs[tab], values, keep_typed=True)
            hint.show_error(problem) if problem else hint.show_argv(argv)
        except (commands.MissingParameter, commands.InvalidValue) as exc:
            hint.show_error(problem or f"Not ready yet: {exc}")
        if tab == "cover":
            self.query_one("#size-cover", Static).update(printprep.cover_preview(values))
        elif tab == "interior":
            trim = [values.get("trim_w"), values.get("trim_h")]
            self.query_one("#size-interior", Static).update(
                f"The exported interior PDF should be {printprep.describe_size(float(trim[0]), float(trim[1]), values.get('unit') or 'mm')} "
                f"(the trim size, within {printprep.DEFAULT_TRIM_TOLERANCE} by default)."
                if all(trim) and _is_number(trim) else "Enter the trim width and height of the finished book.")

    @on(ParamForm.Changed)
    def _form_changed(self, event: ParamForm.Changed) -> None:
        tab = event.form.id[len("form-"):]
        values = event.form.values()
        if tab in self._following:
            if event.name == "input_file" and self._following[tab] and values.get("input_file"):
                event.form.set_value("output_file", printprep.default_output(values["input_file"]))
            elif event.name == "output_file":
                wanted = printprep.default_output(values["input_file"]) if values.get("input_file") else ""
                self._following[tab] = values.get("output_file") in (None, wanted)
        self._refresh(tab)

    # -- running -------------------------------------------------------------------------

    def _run(self, tab: str) -> None:
        values = self._form(tab).values()
        hint = self.query_one(f"#hint-{tab}", CommandHint)
        problem = self._problem(tab, values)
        if problem:
            hint.show_error(problem)
            return
        try:
            argv = commands.argv_for(self.app.ctx, self.specs[tab], values, keep_typed=True)
        except (commands.MissingParameter, commands.InvalidValue) as exc:
            hint.show_error(f"Cannot run yet: {exc}")
            return
        request = RunRequest(" ".join(self.specs[tab].path), argv)
        self._pending = (request, tab, values)
        self.app.begin_run(request)

    @on(RunButton.Pressed, "#run-check")
    def _run_check(self) -> None:
        self._run("check")

    @on(RunButton.Pressed, "#run-interior")
    def _run_interior(self) -> None:
        self._run("interior")

    @on(RunButton.Pressed, "#run-cover")
    def _run_cover(self) -> None:
        self._run("cover")

    def run_finished(self, result) -> None:
        if self._pending is None or self.app.session is None or self.app.session.request is not self._pending[0]:
            return
        _request, tab, values = self._pending
        if tab in ("interior", "cover") and result.returncode == 0 and values.get("output_file"):
            self._produced[tab] = values
            self.query_one(f"#again-{tab}", Button).disabled = False

    def _check_the_result(self, tab: str) -> None:
        values = self._produced.get(tab)
        if not values:
            return
        check = self._form("check")
        check.set_value("input_file", values["output_file"])
        if tab == "interior":  # the interior is checked against the trim size it was set to
            for name in ("trim_w", "trim_h", "unit"):
                check.set_value(name, values.get(name))
        # TabbedContent switches to whichever pane holds the focused widget, so a button that
        # keeps focus inside the old pane would pull the tabs straight back. Let go of the
        # focus first, switch, then focus the field the operator will use next.
        self.set_focus(None)
        self.query_one(TabbedContent).active = "tab-check"
        self.call_after_refresh(lambda: self._form("check").query_one("#param-input_file", Input).focus())
        self._refresh("check")

    @on(Button.Pressed, "#again-interior")
    def _again_interior(self) -> None:
        self._check_the_result("interior")

    @on(Button.Pressed, "#again-cover")
    def _again_cover(self) -> None:
        self._check_the_result("cover")


def _is_number(values) -> bool:
    try:
        [float(v) for v in values]
        return True
    except (TypeError, ValueError):
        return False
