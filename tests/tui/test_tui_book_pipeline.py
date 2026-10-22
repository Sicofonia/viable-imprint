"""ADR 023, T2.4 — running the pipeline (`s2 run`) from the book screen.

The output parser needs no Textual; the screen tests skip without it.
"""
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402
from harness import EXAMPLE_LAUNCHER, GuardedCase, Workspace, ledger, make_book, root_group, run_cli  # noqa: E402
from tui.services.context import Context  # noqa: E402
from tui.services.output import chain_summary  # noqa: E402

logging.getLogger("asyncio").setLevel(logging.ERROR)

CLI_TAIL = [
    "[s2] Running s1b.format...",
    "",
    "Done: 3, Failed: 2",
    "  [x] s1d.brief",
    "  [x] s1d.metadata",
    "  [x] s1b.cleanup",
    "  [!] s1d.synopsis",
    "  [!] s1d.story-map",
    "Book complete — nothing left to run.",
]


class ChainSummaryTests(unittest.TestCase):
    def test_done_and_failed_tasks_are_listed(self):
        summary = chain_summary(CLI_TAIL)
        self.assertEqual(summary.done, ["s1d.brief", "s1d.metadata", "s1b.cleanup"])
        self.assertEqual(summary.failed, ["s1d.synopsis", "s1d.story-map"])
        self.assertEqual(summary.sentence(), "3 tasks done, 2 failed: s1d.synopsis, s1d.story-map.")

    def test_singular_and_all_done(self):
        summary = chain_summary(["Done: 1, Failed: 0", "  [x] s1b.cleanup"])
        self.assertEqual(summary.sentence(), "1 task done, 0 failed.")

    def test_no_block_means_no_summary(self):
        self.assertIsNone(chain_summary(["Created: books/x/", "Saved: y"]))
        self.assertIsNone(chain_summary([]))

    def test_the_last_block_wins_and_stops_at_the_first_other_line(self):
        lines = ["Done: 9, Failed: 9", "  [!] old.one", "Done: 1, Failed: 0", "  [x] new.one", "something else", "  [!] ignored"]
        summary = chain_summary(lines)
        self.assertEqual((summary.done, summary.failed), (["new.one"], []))

    def test_it_reads_real_cli_output(self):
        with Workspace() as ws:
            make_book(ws, "tiny")
            code, out = run_cli(ws, "s2", "run", "tiny", "--step", example=True)
            self.assertEqual(code, 0)
            summary = chain_summary(out.replace("\r\n", "\n").split("\n"))
            self.assertEqual((summary.done, summary.failed), (["s1b.cleanup"], []))


try:
    import textual  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Textual is not installed (uv sync --extra tui)")

from textual.widgets import Checkbox, DataTable, Select, Static  # noqa: E402
from tui.app import PipelineApp  # noqa: E402
from tui.screens.run import RunScreen  # noqa: E402
from tui.widgets.run_button import RunButton  # noqa: E402

SIZE = (150, 50)


async def settle(pilot, times=3):
    for _ in range(times):
        await pilot.pause()


class PipelineRunTests(GuardedCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)

    async def open_book(self, pilot, app, slug="tiny"):
        app.open_book(slug)
        await settle(pilot)
        return app.screen

    async def run_pipeline(self, pilot, app, *, scope=None, step=False):
        screen = app.screen
        if scope:
            screen.query_one("#pipeline-scope", Select).value = scope
        screen.query_one("#pipeline-step", Checkbox).value = step
        await settle(pilot)
        await pilot.click("#run-pipeline")
        await settle(pilot)
        self.assertIsInstance(app.screen, RunScreen)
        if app.screen.state == "confirm":
            await pilot.click("#run-go")
            await settle(pilot)
        await app.session.wait()
        await settle(pilot, 5)
        return app.screen

    async def test_the_hint_shows_the_exact_s2_run_line(self):
        make_book(self.ws, "tiny")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            hint = lambda: screen.query_one("#pipeline-hint").text.rstrip()
            self.assertTrue(hint().endswith(" s2 run tiny"), hint())
            screen.query_one("#pipeline-scope", Select).value = "s1b"
            await settle(pilot)
            self.assertTrue(hint().endswith(" s2 run tiny --only s1b"), hint())
            screen.query_one("#pipeline-step", Checkbox).value = True
            await settle(pilot)
            self.assertTrue(hint().endswith(" s2 run tiny --only s1b --step"), hint())

    async def test_only_s1b_completes_s1b_and_the_summary_says_how_it_went(self):
        root = make_book(self.ws, "tiny")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            screen.query_one("#pipeline-scope", Select).value = "s1b"
            await settle(pilot)
            await pilot.click("#run-pipeline")
            await settle(pilot)
            self.assertEqual(app.screen.state, "confirm")
            text = app.screen.query_one("#run-confirm", Static).render().plain
            self.assertIn("- s1b cleanup: 1 call", text)               # what is ready now
            self.assertIn("More tasks become ready as the run proceeds", text)
            await pilot.click("#run-go")
            await settle(pilot)
            await app.session.wait()
            await settle(pilot, 5)

            entries = ledger(root)
            for name in ("cleanup", "translate", "ortho", "copyedit"):
                self.assertEqual(entries[f"s1b.{name}"]["status"], "done", name)
            self.assertFalse([k for k in entries if k.startswith("s1d.")])
            format_runs = all(p.exists() for p in harness.personal_files("s1b", "format"))
            expected = "5 tasks done, 0 failed." if format_runs else "4 tasks done, 1 failed: s1b.format."
            self.assertIn(expected, app.screen.query_one("#run-summary", Static).render().plain)
            await pilot.press("escape")
            await settle(pilot)
            table = screen.query_one(DataTable)
            self.assertEqual(str(table.get_row("s1b.copyedit")[1]), "done")
            self.assertEqual(str(table.get_row("s1d.brief")[1]), "ready")  # the screen refreshed itself

    async def test_one_step_runs_one_task(self):
        root = make_book(self.ws, "tiny")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            await self.open_book(pilot, app)
            modal = await self.run_pipeline(pilot, app, step=True)
            self.assertEqual(set(ledger(root)), {"s1b.cleanup"})
            self.assertIn("1 task done, 0 failed.", modal.query_one("#run-summary", Static).render().plain)
            self.assertIn("--step", app.session.request.argv)

    async def test_an_injected_failure_is_reported_while_siblings_complete(self):
        root = make_book(self.ws, "tiny")
        code, out = run_cli(self.ws, "s2", "run", "tiny", "--only", "s1b", example=True)
        self.assertEqual(code, 0, out)
        code, out = run_cli(self.ws, "s2", "run", "tiny", "--only", "s1d", "--step", example=True)  # just `brief`
        self.assertEqual(code, 0, out)
        self.ws.update("llm", fail=True)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            await self.open_book(pilot, app)
            modal = await self.run_pipeline(pilot, app, scope="s1d")
            summary = modal.query_one("#run-summary", Static).render().plain
            self.assertIn("exit code 1", summary)
            self.assertIn("1 task done, 6 failed:", summary)
            self.assertIn("s1d.synopsis", summary)
            entries = ledger(root)
            self.assertEqual(entries["s1d.metadata"]["status"], "done")
            self.assertEqual(entries["s1d.synopsis"]["status"], "failed")
            await pilot.press("escape")
            await settle(pilot)
            table = app.screen.query_one(DataTable)
            self.assertEqual(str(table.get_row("s1d.metadata")[1]), "done")
            self.assertEqual(str(table.get_row("s1d.synopsis")[1]), "failed")

    async def test_nothing_ready_in_a_scope_disables_the_button_with_the_reason(self):
        make_book(self.ws, "tiny")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            button = screen.query_one("#run-pipeline", RunButton)
            self.assertFalse(button.disabled)
            screen.query_one("#pipeline-scope", Select).value = "s1d"  # nothing ready: `brief` waits for copyedit
            await settle(pilot)
            self.assertTrue(button.disabled)
            self.assertEqual(button.tooltip, "Nothing is ready to run in this scope")

    async def test_a_finished_book_has_nothing_to_run(self):
        root = make_book(self.ws, "tiny")
        code, out = run_cli(self.ws, "s2", "run", "tiny", example=True)
        if code != 0:  # `format` fails without the personal template; retrying cannot help, so skip the rest
            self.skipTest("the personal format template is missing, so the book cannot finish")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            self.assertTrue(screen.query_one("#run-pipeline", RunButton).disabled)


if __name__ == "__main__":
    unittest.main()
