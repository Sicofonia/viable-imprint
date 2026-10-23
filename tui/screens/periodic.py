"""The periodic pipelines (ADR 023, Phase 6): System 5's homeostat dashboard (T6.1) and
System 1D's monthly newsletter (T6.2), each a chain run as a whole by `s2 ... run`.
The chain's stages are never offered on their own (ADR 023, Verification notes, item 2).
"""
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Checkbox, DataTable, Input, Static, TabbedContent, TabPane

from lib import orchestrator
from tui.screens.base import BaseScreen
from tui.services import commands, estimate, files, state
from tui.services.command_model import build_model
from tui.services.files import open_path
from tui.services.session import RunRequest
from tui.widgets.command_hint import CommandHint
from tui.widgets.run_button import RunButton


class PeriodicScreen(BaseScreen):
    def __init__(self):
        super().__init__()
        self._specs: dict = {}
        self._pending: dict = {}        # request id -> what to do when that run ends

    def compose_body(self) -> ComposeResult:
        with TabbedContent(initial="tab-homeostat", id="periodic-tabs"):
            with TabPane("Homeostat", id="tab-homeostat"):
                with VerticalScroll():
                    yield Static("", id="hs-reads", markup=False)
                    yield DataTable(id="hs-status", cursor_type="row")
                    yield Checkbox("One stage only (the scan, then stop)", id="hs-step")
                    yield CommandHint(id="hs-hint")
                    with Horizontal(classes="periodic-actions"):
                        yield RunButton("Run homeostat chain", id="hs-run")
                        yield Button("Open the homeostat page", id="hs-open", disabled=True)
                    yield Static("Decision log", classes="section", markup=False)
                    yield DataTable(id="hs-decisions", cursor_type="row", zebra_stripes=True)
                    yield Static("Log a decision", classes="section", markup=False)
                    yield Input(id="hs-tension", placeholder="the tension you faced")
                    yield Input(id="hs-decision", placeholder="what you decided")
                    yield Static("", id="hs-log-error", markup=False)
                    yield RunButton("Log decision", id="hs-log")
            with TabPane("Newsletter", id="tab-newsletter"):
                with VerticalScroll():
                    yield DataTable(id="nl-status", cursor_type="row")
                    yield Static("", id="nl-notes-state", markup=False)
                    yield Static("", id="nl-warning", markup=False)
                    yield Button("Open this month's notes", id="nl-notes")
                    yield Checkbox("One stage only (the scan, then stop)", id="nl-step")
                    yield CommandHint(id="nl-hint")
                    with Horizontal(classes="periodic-actions"):
                        yield RunButton("Run newsletter chain", id="nl-run")
                        yield Button("Open the final newsletter", id="nl-open", disabled=True)
                    yield Static("Already featured: explorers", classes="section", markup=False)
                    yield DataTable(id="nl-explorers", cursor_type="row", zebra_stripes=True)
                    yield Static("Already featured: dishes", classes="section", markup=False)
                    yield DataTable(id="nl-dishes", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        model = {tuple(s.path): s for s in build_model(self.app.root)}
        self._specs = {name: model[path] for name, path in (
            ("hs-run", ("s2", "homeostat", "run")), ("hs-log", ("s5", "log-decision")),
            ("nl-run", ("s2", "newsletter", "run")))}
        self.query_one("#hs-status", DataTable).add_columns("Stage", "Last outcome", "Detail")
        self.query_one("#hs-decisions", DataTable).add_columns("Date", "Tension", "Decision")
        self.query_one("#nl-status", DataTable).add_columns("Stage", "Last outcome", "Detail")
        for table in ("#nl-explorers", "#nl-dishes"):
            self.query_one(table, DataTable).add_columns("Name", "Featured on")
        self.refresh_state()
        self._update_hint()
        self._update_newsletter_hint()

    def on_screen_resume(self) -> None:
        self.refresh_state()

    def run_finished(self, result) -> None:
        request = self.app.session.request if self.app.session is not None else None
        after = self._pending.pop(id(request), None)
        if after is not None:
            after(result)
        self.refresh_state()

    # -- state ------------------------------------------------------------------------------

    @property
    def homeostat_root(self):
        return self.app.ctx.data_dir("homeostat_dir")

    def refresh_state(self) -> None:
        ctx = self.app.ctx
        date = state.latest_briefing_date(ctx)
        books = len(state.list_books(ctx))
        self.query_one("#hs-reads", Static).update(
            f"Reads the latest System 4 briefing ({date}) and the System 3 portfolio ({books} book{'s' if books != 1 else ''})."
            if date else
            f"Reads the latest System 4 briefing and the System 3 portfolio ({books} book{'s' if books != 1 else ''}). "
            "No System 4 briefing yet: the homeostat will say so.")

        table = self.query_one("#hs-status", DataTable)
        table.clear()
        for row in state.periodic_rows(self.homeostat_root, orchestrator.HOMEOSTAT_TASKS):
            table.add_row(row.key, row.status, row.detail, key=row.key)
        page = state.rendered_output(self.homeostat_root, orchestrator.HOMEOSTAT_TASKS[-1])
        self.query_one("#hs-open", Button).disabled = page is None

        log = self.query_one("#hs-decisions", DataTable)
        log.clear()
        for d in state.decisions(self.homeostat_root):
            log.add_row(d.get("date", "?"), d.get("tension", ""), d.get("decision", ""))
        self.refresh_newsletter()

    # -- newsletter ------------------------------------------------------------------------------

    @property
    def newsletter_root(self):
        return self.app.ctx.data_dir("newsletter_dir")

    def _notes_warning(self):
        """Why the chain would run without this month's notes, or None."""
        notes = files.notes_state(self.app.ctx)
        if notes == "missing":
            return "There are no notes for this month. The newsletter will be written without them."
        if notes == "empty":
            return "This month's notes file is empty. The newsletter will be written without them."
        return None

    def refresh_newsletter(self) -> None:
        table = self.query_one("#nl-status", DataTable)
        table.clear()
        for row in state.periodic_rows(self.newsletter_root, orchestrator.NEWSLETTER_TASKS):
            table.add_row(row.key, row.status, row.detail, key=row.key)

        notes = files.notes_state(self.app.ctx)
        month = files.this_month()
        detail = notes
        if notes == "ready":
            detail = f"{len(files.notes_path(self.app.ctx).read_text(encoding='utf-8')):,} characters"
        self.query_one("#nl-notes-state", Static).update(f"Notes for {month}: {detail}")
        warning = self._notes_warning()
        self.query_one("#nl-warning", Static).update(f"Warning: {warning}" if warning else "")
        self.query_one("#nl-run", RunButton).label = "Run newsletter chain (no notes!)" if warning else "Run newsletter chain"

        final = state.recorded_output(self.newsletter_root, orchestrator.NEWSLETTER_TASKS[-1])
        self.query_one("#nl-open", Button).disabled = final is None
        for kind, table_id in (("explorers", "#nl-explorers"), ("dishes", "#nl-dishes")):
            featured = self.query_one(table_id, DataTable)
            featured.clear()
            for entry in state.featured(self.newsletter_root, kind):
                featured.add_row(str(entry.get("name", "?")), str(entry.get("date_featured", "?")))

    def _newsletter_values(self) -> dict:
        return {"step": self.query_one("#nl-step", Checkbox).value}

    def _update_newsletter_hint(self) -> None:
        self.query_one("#nl-hint", CommandHint).show_argv(
            commands.argv_for(self.app.ctx, self._specs["nl-run"], self._newsletter_values()))

    @on(Checkbox.Changed, "#nl-step")
    def _newsletter_step_changed(self) -> None:
        self._update_newsletter_hint()

    @on(Button.Pressed, "#nl-notes")
    def _open_notes(self) -> None:
        path = files.ensure_notes_file(self.app.ctx)   # an empty file to write in, if there is none yet
        self.refresh_newsletter()
        open_path(path)

    @on(RunButton.Pressed, "#nl-run")
    def _run_newsletter(self) -> None:
        values = self._newsletter_values()
        argv = commands.argv_for(self.app.ctx, self._specs["nl-run"], values)
        try:
            guess = estimate.estimate_periodic(self.app.ctx, orchestrator.NEWSLETTER_TASKS, step=values["step"])
        except Exception:  # noqa: BLE001 - an estimate must never stop the operator from running
            guess = None
        warning = self._notes_warning()
        self.app.begin_run(RunRequest("s2 newsletter run", argv, estimate=guess, warnings=[warning] if warning else []))

    @on(Button.Pressed, "#nl-open")
    def _open_newsletter(self) -> None:
        final = state.recorded_output(self.newsletter_root, orchestrator.NEWSLETTER_TASKS[-1])
        if final is not None:
            open_path(final)

    # -- running the chain -----------------------------------------------------------------------

    def _chain_values(self) -> dict:
        return {"step": self.query_one("#hs-step", Checkbox).value}

    def _update_hint(self) -> None:
        self.query_one("#hs-hint", CommandHint).show_argv(
            commands.argv_for(self.app.ctx, self._specs["hs-run"], self._chain_values()))

    @on(Checkbox.Changed, "#hs-step")
    def _step_changed(self) -> None:
        self._update_hint()

    @on(RunButton.Pressed, "#hs-run")
    def _run_chain(self) -> None:
        values = self._chain_values()
        argv = commands.argv_for(self.app.ctx, self._specs["hs-run"], values)
        try:
            guess = estimate.estimate_periodic(self.app.ctx, orchestrator.HOMEOSTAT_TASKS, step=values["step"])
        except Exception:  # noqa: BLE001 - an estimate must never stop the operator from running
            guess = None
        self.app.begin_run(RunRequest("s2 homeostat run", argv, estimate=guess))

    @on(Button.Pressed, "#hs-open")
    def _open_page(self) -> None:
        page = state.rendered_output(self.homeostat_root, orchestrator.HOMEOSTAT_TASKS[-1])
        if page is not None:
            open_path(page)

    # -- the decision log -----------------------------------------------------------------------------

    @on(Input.Changed, "#hs-tension")
    @on(Input.Changed, "#hs-decision")
    def _decision_typed(self) -> None:
        self.query_one("#hs-log-error", Static).update("")

    @on(RunButton.Pressed, "#hs-log")
    def _log_decision(self) -> None:
        tension = self.query_one("#hs-tension", Input).value.strip()
        decision = self.query_one("#hs-decision", Input).value.strip()
        if not tension or not decision:
            self.query_one("#hs-log-error", Static).update("Enter both the tension and the decision.")
            return
        argv = commands.argv_for(self.app.ctx, self._specs["hs-log"], {"tension": tension, "decision": decision})
        request = RunRequest("s5 log-decision", argv)
        self._pending[id(request)] = self._clear_decision_fields
        self.app.begin_run(request)

    def _clear_decision_fields(self, result) -> None:
        if result.returncode == 0:
            self.query_one("#hs-tension", Input).value = ""
            self.query_one("#hs-decision", Input).value = ""
