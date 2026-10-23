"""ADR 023, T6.1 — the homeostat tab of the periodic screen. The state helpers need no
Textual; the screen tests skip without it.
"""
import logging
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402
from harness import EXAMPLE_LAUNCHER, GuardedCase, Workspace, ledger, root_group  # noqa: E402
from lib import homeostat, manifest, orchestrator, paths  # noqa: E402
from tui.services import state  # noqa: E402
from tui.services.context import Context  # noqa: E402

logging.getLogger("asyncio").setLevel(logging.ERROR)


def seed_homeostat(ws, *, scan=True, main=None, render=False):
    """Ledger entries for the homeostat stages. `main` is "done", "failed" or None."""
    root = paths.homeostat_root(ws.config)
    if scan:
        manifest.record_task(root, "s5.homeostat-scan", status="done", output="s5/homeostat-scan/2026-10-20/combined.txt",
                             completed_at="2026-10-20T09:00:00+00:00")
    if main == "done":
        manifest.record_task(root, "s5.homeostat", status="done", output="s5/homeostat/2026-10-20/combined.txt",
                             completed_at="2026-10-20T09:01:00+00:00")
    elif main == "failed":
        manifest.record_task(root, "s5.homeostat", status="failed", error="provider said no",
                             attempted_at="2026-10-20T09:01:00+00:00")
    if render:
        html = root / "s5" / "homeostat-render" / "2026-10-20" / "combined.html"
        html.parent.mkdir(parents=True)
        html.write_text("<html></html>", encoding="utf-8")
        manifest.record_task(root, "s5.homeostat-render", status="done", output="s5/homeostat-render/2026-10-20/combined.html",
                             completed_at="2026-10-20T09:02:00+00:00")
    return root


class PeriodicStateTests(GuardedCase):
    def test_never_run_done_and_failed_stages(self):
        with Workspace() as ws:
            ctx = Context.load(ws.config_path)
            rows = state.periodic_rows(ctx.data_dir("homeostat_dir"), orchestrator.HOMEOSTAT_TASKS)
            self.assertEqual([(r.name, r.status) for r in rows], [("homeostat-scan", "never run"),
                                                                    ("homeostat", "never run"),
                                                                    ("homeostat-render", "never run")])
            seed_homeostat(ws, main="failed")
            rows = state.periodic_rows(ctx.data_dir("homeostat_dir"), orchestrator.HOMEOSTAT_TASKS)
            self.assertEqual([r.status for r in rows], ["done", "failed", "never run"])
            self.assertEqual(rows[0].detail, "2026-10-20T09:00:00+00:00  s5/homeostat-scan/2026-10-20/combined.txt")
            self.assertEqual(rows[1].detail, "provider said no")
            self.assertEqual(rows[2].detail, "")

    def test_the_rows_use_the_same_words_as_the_status_command(self):
        with Workspace() as ws:
            seed_homeostat(ws, main="done", render=True)
            code, out = harness.run_cli(ws, "s2", "homeostat", "status", example=True)
            self.assertEqual(code, 0, out)
            ctx = Context.load(ws.config_path)
            for row in state.periodic_rows(ctx.data_dir("homeostat_dir"), orchestrator.HOMEOSTAT_TASKS):
                line = next(l for l in out.splitlines() if re.search(rf"\[x\] {re.escape(row.key)}\s", l))
                self.assertIn(row.detail, line)

    def test_the_rendered_page_is_the_render_stages_recorded_output(self):
        with Workspace() as ws:
            ctx = Context.load(ws.config_path)
            root = ctx.data_dir("homeostat_dir")
            self.assertIsNone(state.rendered_output(root, orchestrator.HOMEOSTAT_TASKS[-1]))
            seed_homeostat(ws, render=True)
            self.assertEqual(state.rendered_output(root, orchestrator.HOMEOSTAT_TASKS[-1]),
                             root / "s5" / "homeostat-render" / "2026-10-20" / "combined.html")
            (root / "s5" / "homeostat-render" / "2026-10-20" / "combined.html").unlink()
            self.assertIsNone(state.rendered_output(root, orchestrator.HOMEOSTAT_TASKS[-1]))  # recorded, but gone

    def test_the_briefing_date_and_the_decision_log(self):
        with Workspace() as ws:
            ctx = Context.load(ws.config_path)
            self.assertIsNone(state.latest_briefing_date(ctx))
            for date in ("2026-10-02", "2026-10-16"):
                path = ctx.data_dir("intelligence_dir") / "s4" / "briefing" / date / "combined.txt"
                path.parent.mkdir(parents=True)
                path.write_text("b", encoding="utf-8")
            self.assertEqual(state.latest_briefing_date(ctx), "2026-10-16")

            root = ctx.data_dir("homeostat_dir")
            self.assertEqual(state.decisions(root), [])
            homeostat.record_decision(root, "cost vs coverage", "scan weekly")
            homeostat.record_decision(root, "speed vs care", "slow down")
            self.assertEqual([d["tension"] for d in state.decisions(root)], ["speed vs care", "cost vs coverage"])  # newest first


try:
    import textual  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Textual is not installed (uv sync --extra tui)")

from textual.widgets import Checkbox, DataTable, Input, Static  # noqa: E402
from tui.app import PipelineApp  # noqa: E402
from tui.screens.periodic import PeriodicScreen  # noqa: E402
from tui.screens.run import RunScreen  # noqa: E402
from tui.widgets.run_button import RunButton  # noqa: E402

SIZE = (170, 90)


async def settle(pilot, times=3):
    for _ in range(times):
        await pilot.pause()


class HomeostatScreenTests(GuardedCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)

    async def open(self, pilot, app):
        await pilot.press("r")
        await settle(pilot)
        self.assertIsInstance(app.screen, PeriodicScreen)
        return app.screen

    def stages(self, screen):
        table = screen.query_one("#hs-status", DataTable)
        return [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]

    async def run_chain(self, pilot, app, *, step=False):
        screen = app.screen
        screen.query_one("#hs-step", Checkbox).value = step
        await settle(pilot)
        await pilot.click("#hs-run")
        await settle(pilot)
        self.assertIsInstance(app.screen, RunScreen)
        if app.screen.state == "confirm":
            await pilot.click("#run-go")
            await settle(pilot)
        await app.session.wait()
        await settle(pilot, 5)

    async def test_the_stages_show_never_run_done_and_failed(self):
        seed_homeostat(self.ws, main="failed")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            rows = self.stages(screen)
            self.assertEqual([(r[0], r[1]) for r in rows], [("s5.homeostat-scan", "done"), ("s5.homeostat", "failed"),
                                                            ("s5.homeostat-render", "never run")])
            self.assertEqual(rows[1][2], "provider said no")

    async def test_it_says_what_the_homeostat_reads_and_which_briefing(self):
        briefing = self.ctx.data_dir("intelligence_dir") / "s4" / "briefing" / "2026-10-16" / "combined.txt"
        briefing.parent.mkdir(parents=True)
        briefing.write_text("b", encoding="utf-8")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            text = screen.query_one("#hs-reads", Static).render().plain
            self.assertIn("latest System 4 briefing", text)
            self.assertIn("System 3 portfolio", text)
            self.assertIn("2026-10-16", text)
        (briefing.parent / "combined.txt").unlink()
        briefing.parent.rmdir()
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            self.assertIn("No System 4 briefing yet", screen.query_one("#hs-reads", Static).render().plain)

    async def test_running_the_chain_renders_the_page_and_the_open_action_targets_it(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            self.assertTrue(screen.query_one("#hs-open").disabled)   # nothing rendered yet
            await pilot.click("#hs-run")
            await settle(pilot)
            self.assertEqual(app.screen.state, "confirm")            # its LLM stage is a provider call
            text = app.screen.query_one("#run-confirm", Static).render().plain
            self.assertIn("- s5 homeostat: 1 call", text)
            await pilot.click("#run-go")
            await settle(pilot)
            await app.session.wait()
            await settle(pilot, 5)
            self.assertIn("3 tasks done, 0 failed.", app.screen.query_one("#run-summary", Static).render().plain)
            await pilot.press("escape")
            await settle(pilot)

            pages = list((self.ws.homeostat_dir).rglob("*.html"))
            self.assertEqual(len(pages), 1)
            self.assertEqual([r[1] for r in self.stages(screen)], ["done", "done", "done"])
            self.assertFalse(screen.query_one("#hs-open").disabled)
            with mock.patch("tui.screens.periodic.open_path") as opened:
                await pilot.click("#hs-open")
                await settle(pilot)
            opened.assert_called_once_with(pages[0])

    async def test_one_stage_only_runs_just_the_scan(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await self.run_chain(pilot, app, step=True)
            self.assertEqual(set(ledger(self.ws.homeostat_dir)), {"s5.homeostat-scan"})
            self.assertTrue(app.session.request.argv[-1] == "--step")
            await pilot.press("escape")
            await settle(pilot)
            self.assertEqual([r[1] for r in self.stages(screen)], ["done", "never run", "never run"])

    async def test_a_failed_first_stage_leaves_the_later_stages_not_run(self):
        self.ws.update("llm", fail=True)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await self.run_chain(pilot, app)
            self.assertIn("1 task done, 1 failed: s5.homeostat.", app.screen.query_one("#run-summary", Static).render().plain)
            await pilot.press("escape")
            await settle(pilot)
            rows = self.stages(screen)
            self.assertEqual([r[1] for r in rows], ["done", "failed", "never run"])
            self.assertIn("configured to fail", rows[1][2])
            self.assertTrue(screen.query_one("#hs-open").disabled)

    async def test_logging_a_decision_appears_in_the_list(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            table = screen.query_one("#hs-decisions", DataTable)
            self.assertEqual(table.row_count, 0)
            screen.query_one("#hs-tension", Input).value = "cost vs. coverage"
            await pilot.click("#hs-log")
            await settle(pilot)
            self.assertIs(app.screen, screen)   # both fields are needed
            self.assertIn("both", screen.query_one("#hs-log-error", Static).render().plain)
            screen.query_one("#hs-decision", Input).value = "scan weekly"
            await settle(pilot)
            await pilot.click("#hs-log")
            await settle(pilot)
            await app.session.wait()
            await settle(pilot, 5)
            await pilot.press("escape")
            await settle(pilot)
            self.assertEqual(table.row_count, 1)
            row = [str(c) for c in table.get_row_at(0)]
            self.assertEqual(row[1:], ["cost vs. coverage", "scan weekly"])
            self.assertEqual(homeostat.load_decisions(self.ws.homeostat_dir)[0]["decision"], "scan weekly")
            self.assertEqual(screen.query_one("#hs-tension", Input).value, "")   # cleared for the next one


if __name__ == "__main__":
    unittest.main()
