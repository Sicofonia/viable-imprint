"""ADR 023, T6.2 — the newsletter tab of the periodic screen. The helpers need no Textual;
the screen tests skip without it.
"""
import datetime
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402
from harness import EXAMPLE_LAUNCHER, GuardedCase, Workspace, ledger, root_group  # noqa: E402
from lib import manifest, orchestrator  # noqa: E402
from tui.services import files, state  # noqa: E402
from tui.services.context import Context  # noqa: E402
from tui.services.output import warning_lines  # noqa: E402

logging.getLogger("asyncio").setLevel(logging.ERROR)

FROZEN = datetime.date(2026, 3, 5)


class NotesFileTests(GuardedCase):
    def test_the_notes_file_is_created_empty_at_the_right_path_for_a_frozen_date(self):
        with Workspace() as ws, mock.patch.object(files, "today", lambda: FROZEN):
            ctx = Context.load(ws.config_path)
            self.assertEqual(files.this_month(), "2026-03")
            self.assertEqual(files.notes_state(ctx), "missing")
            self.assertFalse((ws.newsletter_dir / "notes").exists())   # looking creates nothing
            path = files.ensure_notes_file(ctx)
            self.assertEqual(path, ws.newsletter_dir / "notes" / "2026-03.txt")
            self.assertEqual(path.read_text(encoding="utf-8"), "")
            self.assertEqual(files.notes_state(ctx), "empty")

    def test_an_existing_notes_file_is_never_overwritten(self):
        with Workspace() as ws, mock.patch.object(files, "today", lambda: FROZEN):
            ctx = Context.load(ws.config_path)
            path = files.ensure_notes_file(ctx)
            path.write_text("Notas del mes.", encoding="utf-8")
            self.assertEqual(files.ensure_notes_file(ctx), path)
            self.assertEqual(path.read_text(encoding="utf-8"), "Notas del mes.")
            self.assertEqual(files.notes_state(ctx), "ready")

    def test_whitespace_only_notes_count_as_empty(self):
        with Workspace() as ws, mock.patch.object(files, "today", lambda: FROZEN):
            ctx = Context.load(ws.config_path)
            files.ensure_notes_file(ctx).write_text("  \n\n ", encoding="utf-8")
            self.assertEqual(files.notes_state(ctx), "empty")   # newsletter_scan strips the text, too

    def test_a_different_month_is_a_different_file(self):
        with Workspace() as ws:
            ctx = Context.load(ws.config_path)
            with mock.patch.object(files, "today", lambda: datetime.date(2026, 3, 31)):
                march = files.ensure_notes_file(ctx)
            with mock.patch.object(files, "today", lambda: datetime.date(2026, 4, 1)):
                april = files.ensure_notes_file(ctx)
            self.assertNotEqual(march, april)
            self.assertEqual((march.name, april.name), ("2026-03.txt", "2026-04.txt"))


class FeaturedListTests(GuardedCase):
    def test_the_lists_match_the_tracking_files_newest_first(self):
        with Workspace() as ws:
            ctx = Context.load(ws.config_path)
            root = ctx.data_dir("newsletter_dir")
            self.assertEqual(state.featured(root, "explorers"), [])
            entries = [{"name": "Isabella Bird", "date_featured": "2026-01-10"},
                       {"name": "Nikolai Przhevalsky", "date_featured": "2026-02-10"}]
            (root / "featured_explorers.yaml").write_text(yaml.safe_dump(entries), encoding="utf-8")
            self.assertEqual([e["name"] for e in state.featured(root, "explorers")],
                             ["Nikolai Przhevalsky", "Isabella Bird"])
            self.assertEqual(state.featured(root, "dishes"), [])

    def test_a_corrupt_file_is_an_empty_list_not_a_crash(self):
        with Workspace() as ws:
            root = Context.load(ws.config_path).data_dir("newsletter_dir")
            (root / "featured_dishes.yaml").write_text("- name: [unclosed", encoding="utf-8")
            self.assertEqual(state.featured(root, "dishes"), [])


class WarningLineTests(unittest.TestCase):
    def test_warning_lines_from_a_run(self):
        lines = ["[s2] Running s1d.newsletter-track...",
                 "  Warning: 'Exploradora de Prueba' appears to repeat a previous explorer of the month — check before sending.",
                 "Warning: no notes", "Saved: x", "  Warning: another"]
        self.assertEqual([w[:20] for w in warning_lines(lines)],
                         ["'Exploradora de Prue", "no notes", "another"])

    def test_none(self):
        self.assertEqual(warning_lines(["Saved: x", "Done: 1, Failed: 0"]), [])


try:
    import textual  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Textual is not installed (uv sync --extra tui)")

from textual.widgets import Button, Checkbox, DataTable, Static, TabbedContent  # noqa: E402
from tui.app import PipelineApp  # noqa: E402
from tui.screens.periodic import PeriodicScreen  # noqa: E402
from tui.screens.run import RunScreen  # noqa: E402
from tui.widgets.run_button import RunButton  # noqa: E402

SIZE = (170, 90)


async def settle(pilot, times=3):
    for _ in range(times):
        await pilot.pause()


class NewsletterScreenTests(GuardedCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)

    def write_notes(self, text="Una nota del mes."):
        path = self.ws.newsletter_dir / "notes" / f"{datetime.date.today().isoformat()[:7]}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    async def open(self, pilot, app):
        await pilot.press("r")
        await settle(pilot)
        self.assertIsInstance(app.screen, PeriodicScreen)
        # switch with the keyboard-free route: the tab is a programmatic choice, nothing is focused in the old pane
        app.screen.set_focus(None)
        app.screen.query_one(TabbedContent).active = "tab-newsletter"
        await settle(pilot, 5)
        return app.screen

    def table(self, screen, table_id):
        table = screen.query_one(f"#{table_id}", DataTable)
        return [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]

    def text(self, screen, widget_id):
        return screen.query_one(f"#{widget_id}", Static).render().plain

    async def run_chain(self, pilot, app, *, confirm=True):
        await pilot.click("#nl-run")
        await settle(pilot)
        self.assertIsInstance(app.screen, RunScreen)
        if confirm:
            self.assertEqual(app.screen.state, "confirm")
            await pilot.click("#run-go")
            await settle(pilot)
        await app.session.wait()
        await settle(pilot, 5)

    async def test_the_stage_status_lists_all_three_stages(self):
        root = self.ws.newsletter_dir
        manifest.save(root, {"slug": "newsletter"})
        manifest.record_task(root, "s1d.newsletter-scan", status="done", output="s1d/newsletter-scan/2026-10-20/combined.txt",
                             completed_at="2026-10-20T09:00:00+00:00")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            rows = self.table(screen, "nl-status")
            self.assertEqual([(r[0], r[1]) for r in rows], [("s1d.newsletter-scan", "done"), ("s1d.newsletter", "never run"),
                                                            ("s1d.newsletter-track", "never run")])

    async def test_opening_the_notes_creates_this_months_file_and_opens_it(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            with mock.patch.object(files, "today", lambda: FROZEN), mock.patch("tui.screens.periodic.open_path") as opened:
                await pilot.click("#nl-notes")
                await settle(pilot)
            expected = self.ws.newsletter_dir / "notes" / "2026-03.txt"
            self.assertTrue(expected.is_file())
            self.assertEqual(expected.read_text(encoding="utf-8"), "")
            opened.assert_called_once_with(expected)

    async def test_missing_or_empty_notes_warn_on_the_run_button_and_in_the_confirmation(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            self.assertIn("missing", self.text(screen, "nl-notes-state"))
            self.assertIn("without them", self.text(screen, "nl-warning"))
            self.assertIn("no notes", str(screen.query_one("#nl-run", RunButton).label))
            await pilot.click("#nl-run")
            await settle(pilot)
            confirm = app.screen.query_one("#run-confirm", Static).render().plain
            self.assertIn("! There are no notes for this month", confirm)
            self.assertIn("- s1d newsletter: 1 call", confirm)
            await pilot.click("#run-close")
            await settle(pilot)

            self.write_notes("   ")
            screen.refresh_state()
            await settle(pilot)
            self.assertIn("empty", self.text(screen, "nl-notes-state"))
            self.assertIn("without them", self.text(screen, "nl-warning"))

            self.write_notes("Una nota del mes.")
            screen.refresh_state()
            await settle(pilot)
            self.assertEqual(self.text(screen, "nl-warning"), "")
            self.assertNotIn("no notes", str(screen.query_one("#nl-run", RunButton).label))
            self.assertIn("17 characters", self.text(screen, "nl-notes-state"))

    async def test_the_chain_runs_lists_the_featured_names_and_opens_the_final_newsletter(self):
        self.write_notes()
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await self.run_chain(pilot, app)
            summary = app.screen.query_one("#run-summary", Static).render().plain
            self.assertIn("3 tasks done, 0 failed.", summary)
            self.assertNotIn("Warnings", summary)    # first time: nothing repeats
            await pilot.press("escape")
            await settle(pilot)

            self.assertEqual([r[1] for r in self.table(screen, "nl-status")], ["done", "done", "done"])
            self.assertEqual([r[0] for r in self.table(screen, "nl-explorers")], ["Exploradora de Prueba"])
            self.assertEqual([r[0] for r in self.table(screen, "nl-dishes")], ["Plato de Prueba"])
            on_disk = yaml.safe_load((self.ws.newsletter_dir / "featured_explorers.yaml").read_text(encoding="utf-8"))
            self.assertEqual([e["name"] for e in on_disk], ["Exploradora de Prueba"])

            final = self.ws.newsletter_dir / ledger(self.ws.newsletter_dir)["s1d.newsletter-track"]["output"]
            with mock.patch("tui.screens.periodic.open_path") as opened:
                await pilot.click("#nl-open")
                await settle(pilot)
            opened.assert_called_once_with(final)

    async def test_a_repeated_explorer_is_highlighted_in_the_run_summary(self):
        self.write_notes()
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await self.run_chain(pilot, app)
            await pilot.press("escape")
            await settle(pilot)
            await self.run_chain(pilot, app)
            summary = app.screen.query_one("#run-summary", Static).render().plain
            self.assertIn("Warnings:", summary)
            self.assertIn("appears to repeat a previous explorer", summary)
            self.assertIn("appears to repeat a previous dish", summary)
            self.assertIn("3 tasks done, 0 failed.", summary)   # still a success: advisory only
            await pilot.press("escape")
            await settle(pilot)
            self.assertEqual(len(self.table(screen, "nl-explorers")), 1)   # not recorded twice

    async def test_one_stage_only_runs_the_scan_without_asking(self):
        self.write_notes()
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            screen.query_one("#nl-step", Checkbox).value = True
            await settle(pilot)
            self.assertTrue(screen.query_one("#nl-hint").text.rstrip().endswith("s2 newsletter run --step"))
            await self.run_chain(pilot, app, confirm=False)
            self.assertEqual(set(ledger(self.ws.newsletter_dir)), {"s1d.newsletter-scan"})

    async def test_the_open_button_waits_for_a_finished_newsletter(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            self.assertTrue(screen.query_one("#nl-open", Button).disabled)


if __name__ == "__main__":
    unittest.main()
