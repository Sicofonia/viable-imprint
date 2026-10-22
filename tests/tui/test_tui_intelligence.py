"""ADR 023, T5.1 — the intelligence screen (S4 scan, briefing, content strategy, and the
S1D article draft). The dated-folder resolution needs no Textual; the screen tests skip
without it.
"""
import asyncio
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402
from harness import EXAMPLE_LAUNCHER, GuardedCase, Workspace, fixture, root_group  # noqa: E402
from lib import paths  # noqa: E402
from tui.services import files  # noqa: E402
from tui.services.context import Context  # noqa: E402

logging.getLogger("asyncio").setLevel(logging.ERROR)


def make_output(root: Path, system: str, stage: str, date: str, text: str = "x") -> Path:
    path = root / system / stage / date / "combined.txt"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


class DatedOutputTests(GuardedCase):
    def test_newest_first_and_only_folders_that_hold_the_output(self):
        with Workspace() as ws:
            root = paths.intelligence_root(ws.config)
            for date in ("2026-09-30", "2026-10-15", "2026-10-02"):
                make_output(root, "s4", "scan", date)
            (root / "s4" / "scan" / "2026-10-20").mkdir()               # a run that failed before writing
            (root / "s4" / "scan" / "_state.yaml").write_text("{}", encoding="utf-8")   # not a dated folder
            found = files.dated_outputs(root, "s4", "scan")
            self.assertEqual([o.date for o in found], ["2026-10-15", "2026-10-02", "2026-09-30"])
            self.assertEqual(found[0].path, root / "s4" / "scan" / "2026-10-15" / "combined.txt")

    def test_a_stage_that_never_ran_has_no_outputs(self):
        with Workspace() as ws:
            root = paths.intelligence_root(ws.config)
            self.assertEqual(files.dated_outputs(root, "s4", "briefing"), [])
            self.assertEqual(files.dated_outputs(root, "s1d", "article-draft"), [])

    def test_the_date_comparison_is_by_name_not_by_modification_time(self):
        with Workspace() as ws:
            root = paths.intelligence_root(ws.config)
            newer = make_output(root, "s4", "briefing", "2026-10-15")
            older = make_output(root, "s4", "briefing", "2026-09-01")
            import os
            os.utime(newer, (1, 1))  # touched long ago, but still the latest briefing
            self.assertEqual(files.dated_outputs(root, "s4", "briefing")[0].path, newer)
            self.assertNotEqual(files.dated_outputs(root, "s4", "briefing")[0].path, older)


try:
    import textual  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Textual is not installed (uv sync --extra tui)")

from textual.widgets import Button, DataTable, Static  # noqa: E402
from tui.app import PipelineApp  # noqa: E402
from tui.screens.intelligence import IntelligenceScreen  # noqa: E402
from tui.screens.run import RunScreen  # noqa: E402
from tui.services.runner import RunResult  # noqa: E402
from tui.widgets.run_button import RunButton  # noqa: E402

SIZE = (170, 90)


async def settle(pilot, times=3):
    for _ in range(times):
        await pilot.pause()


class IntelligenceCase(GuardedCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)
        self.root = paths.intelligence_root(self.ws.config)

    async def open(self, pilot, app):
        await pilot.press("i")
        await settle(pilot)
        self.assertIsInstance(app.screen, IntelligenceScreen)
        return app.screen

    def rows(self, screen, table_id):
        table = screen.query_one(f"#{table_id}", DataTable)
        return [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]

    async def run_to_the_end(self, pilot, app, confirm=False):
        await settle(pilot)
        self.assertIsInstance(app.screen, RunScreen)
        if confirm and app.screen.state == "confirm":
            await pilot.click("#run-go")
            await settle(pilot)
        await app.session.wait()
        await settle(pilot, 5)
        await pilot.press("escape")
        await settle(pilot)


class ListsTests(IntelligenceCase):
    async def test_the_four_lists_are_newest_first_with_the_date_of_the_last_scan(self):
        for date in ("2026-10-02", "2026-10-15"):
            make_output(self.root, "s4", "scan", date)
        make_output(self.root, "s4", "briefing", "2026-10-16")
        make_output(self.root, "s4", "content-strategy", "2026-10-17")
        make_output(self.root, "s1d", "article-draft", "2026-10-18")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            self.assertEqual([r[0] for r in self.rows(screen, "scans")], ["2026-10-15", "2026-10-02"])
            self.assertEqual([r[0] for r in self.rows(screen, "briefings")], ["2026-10-16"])
            self.assertEqual([r[0] for r in self.rows(screen, "strategies")], ["2026-10-17"])
            self.assertEqual([r[0] for r in self.rows(screen, "drafts")], ["2026-10-18"])
            self.assertIn("2026-10-15", screen.query_one("#last-scan", Static).render().plain)

    async def test_with_nothing_yet_it_says_so_and_the_later_stages_are_blocked(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            self.assertIn("No scan yet", screen.query_one("#last-scan", Static).render().plain)
            self.assertFalse(screen.query_one("#run-scan", RunButton).disabled)
            for button, reason in (("run-briefing", "Run a scan first"), ("run-both", None),
                                   ("run-strategy", "Run a briefing first"), ("run-draft", "Run a content strategy first")):
                widget = screen.query_one(f"#{button}", RunButton)
                if reason:
                    self.assertTrue(widget.disabled, button)
                    self.assertEqual(widget.tooltip, reason, button)
            self.assertFalse(screen.query_one("#run-both", RunButton).disabled)  # a scan can start the chain

    async def test_each_list_opens_its_selected_output(self):
        briefing = make_output(self.root, "s4", "briefing", "2026-10-16")
        newer = make_output(self.root, "s4", "briefing", "2026-10-20")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            with mock.patch("tui.screens.intelligence.open_path") as opened:
                await pilot.click("#open-briefing")
                await settle(pilot)
                screen.query_one("#briefings", DataTable).move_cursor(row=1)
                await settle(pilot)
                await pilot.click("#open-briefing")
                await settle(pilot)
            self.assertEqual([c.args[0] for c in opened.call_args_list], [newer, briefing])


class RunTests(IntelligenceCase):
    async def test_the_first_scan_warns_once_and_a_later_one_does_not(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await pilot.click("#run-scan")
            await settle(pilot)
            self.assertEqual(app.screen.state, "confirm")  # a warning forces the question
            text = app.screen.query_one("#run-confirm", Static).render().plain
            self.assertIn("first scan", text)
            self.assertIn("many old items", text)
            await pilot.click("#run-go")
            await settle(pilot)
            await app.session.wait()
            await settle(pilot, 5)
            await pilot.press("escape")
            await settle(pilot)
            self.assertEqual(len(self.rows(screen, "scans")), 1)  # the list refreshed

            await pilot.click("#run-scan")
            await settle(pilot)
            self.assertNotEqual(app.screen.state, "confirm")  # no warning, no estimate: it just runs

    async def test_scan_then_briefing_then_content_strategy_then_article_draft(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await pilot.click("#run-scan")
            await self.run_to_the_end(pilot, app, confirm=True)
            scan = files.dated_outputs(self.root, "s4", "scan")[0]
            self.assertIn("Tiny test feed", scan.path.read_text(encoding="utf-8"))

            await pilot.click("#run-briefing")
            await self.run_to_the_end(pilot, app, confirm=True)
            briefing = files.dated_outputs(self.root, "s4", "briefing")[0]

            await pilot.click("#run-strategy")
            await self.run_to_the_end(pilot, app, confirm=True)
            strategy = files.dated_outputs(self.root, "s4", "content-strategy")[0]

            await pilot.click("#run-draft")
            await self.run_to_the_end(pilot, app, confirm=True)
            draft = files.dated_outputs(self.root, "s1d", "article-draft")[0]

            for found in (scan, briefing, strategy, draft):
                self.assertTrue(found.path.is_file())
            for table, expected in (("scans", 1), ("briefings", 1), ("strategies", 1), ("drafts", 1)):
                self.assertEqual(len(self.rows(screen, table)), expected, table)

    async def test_each_stage_works_on_the_selected_output_of_the_one_before(self):
        make_output(self.root, "s4", "scan", "2026-10-02", "older scan")
        newest = make_output(self.root, "s4", "scan", "2026-10-15", "newer scan")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            with mock.patch.object(app, "begin_run") as begin:
                await pilot.click("#run-briefing")
                await settle(pilot)
                self.assertEqual(begin.call_args.args[0].argv[-1], str(newest))   # newest by default
                self.assertIn("2026-10-15", str(screen.query_one("#run-briefing", RunButton).label))
                screen.query_one("#scans", DataTable).move_cursor(row=1)
                await settle(pilot)
                await pilot.click("#run-briefing")
                await settle(pilot)
                self.assertTrue(begin.call_args.args[0].argv[-1].endswith(str(Path("2026-10-02") / "combined.txt")))
                self.assertIn("2026-10-02", str(screen.query_one("#run-briefing", RunButton).label))
            request = begin.call_args.args[0]
            self.assertEqual(request.estimate.calls, 1)   # a provider call: it will ask first

    async def test_scan_and_brief_runs_both_in_sequence(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            await self.open(pilot, app)
            await pilot.click("#run-both")
            await settle(pilot)
            if app.screen.state == "confirm":   # the first-scan warning
                await pilot.click("#run-go")
                await settle(pilot)
            await app.session.wait()
            await settle(pilot, 4)
            # the scan ended and the briefing was offered next, with its provider confirmation
            self.assertIsInstance(app.screen, RunScreen)
            self.assertEqual(app.screen.request.title, "s4 briefing")
            self.assertEqual(app.screen.state, "confirm")
            await pilot.click("#run-go")
            await settle(pilot)
            await app.session.wait()
            await settle(pilot, 5)
            self.assertEqual(len(files.dated_outputs(self.root, "s4", "scan")), 1)
            self.assertEqual(len(files.dated_outputs(self.root, "s4", "briefing")), 1)

    async def test_scan_and_brief_stops_when_the_scan_fails(self):
        make_output(self.root, "s4", "scan", "2026-10-01")   # a scan exists, so no first-scan question
        app = PipelineApp(self.ctx, root_group())
        failed = RunResult(argv=[], returncode=1, lines=["Error: boom"])
        async with app.run_test(size=SIZE) as pilot:
            await self.open(pilot, app)
            with mock.patch.object(app.runner, "run", mock.AsyncMock(return_value=failed)) as runner_run:
                await pilot.click("#run-both")
                await settle(pilot)
                await app.session.wait()
                await settle(pilot, 5)
            self.assertEqual(runner_run.await_count, 1)                    # the briefing never started
            self.assertEqual(app.session.request.title, "s4 scan")
            self.assertEqual(len(files.dated_outputs(self.root, "s4", "briefing")), 0)
            self.assertIsInstance(app.screen, RunScreen)                   # the failed scan's log stays on screen


if __name__ == "__main__":
    unittest.main()
