"""ADR 023, T2.3 — running a single task from the book screen. Needs Textual."""
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

try:
    import textual  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Textual is not installed (uv sync --extra tui)")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402
from harness import EXAMPLE_LAUNCHER, GuardedCase, Workspace, fixture, ledger, make_book, root_group, run_cli  # noqa: E402
from lib import manifest  # noqa: E402
from textual.widgets import DataTable, Static  # noqa: E402
from tui.app import PipelineApp  # noqa: E402
from tui.screens.book import BookScreen  # noqa: E402
from tui.screens.run import RunScreen  # noqa: E402
from tui.services.context import Context  # noqa: E402
from tui.widgets.param_form import ParamForm  # noqa: E402
from tui.widgets.run_button import RunButton  # noqa: E402

logging.getLogger("asyncio").setLevel(logging.ERROR)
SIZE = (150, 45)


async def settle(pilot, times=3):
    for _ in range(times):
        await pilot.pause()


def seed_done(root, *names):
    """Mark s1b tasks done with real output files, as if they had run."""
    outputs = {"cleanup": "s1b/cleaned/tiny_source.txt", "translate": "s1b/translated/es/tiny_source.txt",
               "ortho": "s1b/ortho/es/tiny_source.txt", "copyedit": "s1b/copyedit/es/tiny_source.txt"}
    for name in names:
        path = root / outputs[name]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("CAPITULO I.\n\nUn parrafo.\n", encoding="utf-8")
        manifest.record_task(root, f"s1b.{name}", status="done", output=outputs[name],
                             completed_at="2026-10-01T10:00:00+00:00", output_mtime=path.stat().st_mtime)


class BookRunCase(GuardedCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)

    async def open_book(self, pilot, app, slug="tiny"):
        app.open_book(slug)
        await settle(pilot)
        self.assertIsInstance(app.screen, BookScreen)
        return app.screen

    async def select(self, pilot, screen, key):
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index(key))
        await settle(pilot)
        self.assertEqual(screen.selected().key, key)

    async def finish_run(self, pilot, app, *, confirm=True):
        await pilot.click("#run-task")
        await settle(pilot)
        self.assertIsInstance(app.screen, RunScreen)
        if confirm:
            self.assertEqual(app.screen.state, "confirm")
            await pilot.click("#run-go")
            await settle(pilot)
        await app.session.wait()
        await settle(pilot, 5)
        await pilot.press("escape")  # close the log
        await settle(pilot)


class RunOneTaskTests(BookRunCase):
    async def test_cleanup_then_translate_from_the_ui_record_both_done(self):
        root = make_book(self.ws, "tiny")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            await self.select(pilot, screen, "s1b.cleanup")
            await self.finish_run(pilot, app)
            self.assertEqual(ledger(root)["s1b.cleanup"]["status"], "done")
            table = screen.query_one(DataTable)
            self.assertEqual(str(table.get_row("s1b.cleanup")[1]), "done")
            self.assertEqual(str(table.get_row("s1b.translate")[1]), "ready")

            await self.select(pilot, screen, "s1b.translate")
            await self.finish_run(pilot, app)
            self.assertEqual({k: v["status"] for k, v in ledger(root).items()},
                             {"s1b.cleanup": "done", "s1b.translate": "done"})
            self.assertEqual(ledger(root)["s1b.translate"]["provider"], "fake")
            self.assertEqual(str(table.get_row("s1b.ortho")[1]), "ready")

    async def test_the_command_hint_is_exactly_what_will_run(self):
        make_book(self.ws, "tiny")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            await self.select(pilot, screen, "s1b.cleanup")
            hint = screen.query_one("#task-hint").text
            self.assertTrue(hint.rstrip().endswith(
                str(self.ws.book("tiny") / "s1b" / "source" / "tiny_source.txt")), hint)
            self.assertIn("s1b cleanup", hint)

    async def test_the_confirmation_names_the_provider_and_declining_runs_nothing(self):
        root = make_book(self.ws, "tiny")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            await self.select(pilot, screen, "s1b.cleanup")
            await pilot.click("#run-task")
            await settle(pilot)
            self.assertEqual(app.screen.state, "confirm")
            text = app.screen.query_one("#run-confirm", Static).render().plain
            self.assertIn("Provider: fake / fake-1", text)
            self.assertIn("Provider calls: about 1", text)
            await pilot.click("#run-close")  # Cancel
            await settle(pilot)
            self.assertIs(app.screen, screen)
            self.assertIsNone(app.session)
            self.assertEqual(ledger(root), {})

    async def test_a_task_without_a_provider_runs_without_asking(self):
        root = make_book(self.ws, "tiny")
        seed_done(root, "cleanup", "translate", "ortho", "copyedit")
        brief = root / "s1d" / "brief" / "es" / "tiny_source.txt"
        brief.parent.mkdir(parents=True)
        brief.write_text("## Brief\n\n<overview>Una descripcion.</overview>\n", encoding="utf-8")
        manifest.record_task(root, "s1d.brief", status="done", output="s1d/brief/es/tiny_source.txt",
                             completed_at="2026-10-01T10:00:00+00:00", output_mtime=brief.stat().st_mtime)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            await self.select(pilot, screen, "s1d.metadata")
            await self.finish_run(pilot, app, confirm=False)
            self.assertEqual(ledger(root)["s1d.metadata"]["status"], "done")
            self.assertNotIn("usage", ledger(root)["s1d.metadata"])

    async def test_blocked_rows_have_a_disabled_run_button_that_says_why(self):
        make_book(self.ws, "tiny")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            button = screen.query_one("#run-task", RunButton)
            await self.select(pilot, screen, "s1b.cleanup")
            self.assertFalse(button.disabled)
            await self.select(pilot, screen, "s1b.translate")
            self.assertTrue(button.disabled)
            self.assertEqual(button.tooltip, "waiting on s1b.cleanup")
            await pilot.click("#run-task")  # a disabled button does nothing
            await settle(pilot)
            self.assertIs(app.screen, screen)

    async def test_no_source_file_blocks_the_first_task_with_the_reason(self):
        make_book(self.ws, "empty", source=False)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app, "empty")
            await self.select(pilot, screen, "s1b.cleanup")
            button = screen.query_one("#run-task", RunButton)
            self.assertTrue(button.disabled)
            self.assertEqual(button.tooltip, "no file in s1b/source/")

    async def test_a_failed_task_can_be_retried(self):
        root = make_book(self.ws, "tiny")
        manifest.record_task(root, "s1b.cleanup", status="failed", error="boom", attempted_at="2026-10-01T00:00:00+00:00")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            await self.select(pilot, screen, "s1b.cleanup")
            self.assertEqual(str(screen.query_one("#run-task", RunButton).label), "Retry")
            await self.finish_run(pilot, app)
            self.assertEqual(ledger(root)["s1b.cleanup"]["status"], "done")

    async def test_a_run_that_fails_is_shown_as_failed_on_the_screen(self):
        root = make_book(self.ws, "tiny")
        self.ws.update("llm", fail=True)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            await self.select(pilot, screen, "s1b.cleanup")
            await self.finish_run(pilot, app)
            row = [str(c) for c in screen.query_one(DataTable).get_row("s1b.cleanup")]
            self.assertEqual(row[1], "failed")
            self.assertIn("configured to fail", row[2])
            self.assertEqual(ledger(root)["s1b.cleanup"]["status"], "failed")


class RerunTests(BookRunCase):
    async def test_rerunning_a_done_task_warns_and_then_marks_downstream_stale(self):
        root = make_book(self.ws, "tiny")
        code, out = run_cli(self.ws, "s2", "run", "tiny", "--only", "s1b", example=True)
        self.assertEqual(code, 0, out)
        done_before = {k for k, v in ledger(root).items() if v["status"] == "done"}
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            await self.select(pilot, screen, "s1b.cleanup")
            self.assertEqual(str(screen.query_one("#run-task", RunButton).label), "Run again")
            await pilot.click("#run-task")
            await settle(pilot)
            self.assertEqual(app.screen.state, "confirm")
            text = app.screen.query_one("#run-confirm", Static).render().plain
            self.assertIn("has already run. Running it again overwrites s1b", text)
            stale_line = next(line for line in text.splitlines() if "will be marked stale" in line)
            for name in ("translate", "ortho", "copyedit"):
                self.assertIn(name, stale_line)
            self.assertNotIn("cleanup,", stale_line)
            self.assertIn("Provider calls: about 1", text)
            await pilot.click("#run-go")
            await settle(pilot)
            await app.session.wait()
            await settle(pilot, 5)
            await pilot.press("escape")
            await settle(pilot)

            table = screen.query_one(DataTable)
            self.assertEqual(str(table.get_row("s1b.cleanup")[1]), "done")
            for name in ("translate", "ortho", "copyedit"):
                self.assertEqual(str(table.get_row(f"s1b.{name}")[1]), "stale", name)
            self.assertEqual({k for k, v in ledger(root).items() if v["status"] == "stale"},
                             done_before - {"s1b.cleanup"})

    async def test_rerunning_a_done_task_nobody_depends_on_warns_only_about_the_overwrite(self):
        root = make_book(self.ws, "tiny")
        seed_done(root, "cleanup")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            await self.select(pilot, screen, "s1b.cleanup")
            await pilot.click("#run-task")
            await settle(pilot)
            text = app.screen.query_one("#run-confirm", Static).render().plain
            self.assertIn("overwrites", text)
            self.assertNotIn("marked stale", text)


class EngineOptionsTests(BookRunCase):
    async def run_and_capture(self, pilot, app, screen):
        with mock.patch.object(app, "begin_run") as begin:
            await pilot.click("#run-task")
            await settle(pilot)
        begin.assert_called_once()
        return begin.call_args.args[0]

    async def test_the_template_option_reaches_the_command(self):
        root = make_book(self.ws, "tiny")
        seed_done(root, "cleanup", "translate", "ortho", "copyedit")
        template = str(fixture("tiny_source.txt"))  # any existing file: the command is only built, not run
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            await self.select(pilot, screen, "s1b.format")
            form = screen.query_one("#task-options", ParamForm)
            self.assertEqual([p.name for p in form.spec.params], ["template"])  # the input file is resolved, not typed
            request = await self.run_and_capture(pilot, app, screen)
            self.assertNotIn("--template", request.argv)
            form.set_value("template", template)
            await settle(pilot)
            self.assertIn(f"--template {template}", screen.query_one("#task-hint").text)
            request = await self.run_and_capture(pilot, app, screen)
            self.assertEqual(request.argv[-2:], ["--template", template])
            self.assertEqual(request.argv[-3], str(root / "s1b" / "copyedit" / "es" / "tiny_source.txt"))

    async def test_the_document_flag_reaches_the_command_and_the_estimate(self):
        root = make_book(self.ws, "tiny")
        seed_done(root, "cleanup")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            await self.select(pilot, screen, "s1b.translate")
            screen.query_one("#task-options", ParamForm).set_value("document", True)
            await settle(pilot)
            request = await self.run_and_capture(pilot, app, screen)
            self.assertIn("--document", request.argv)
            self.assertEqual(request.estimate.calls, 1)

    async def test_etranslation_confirmation_states_the_wait_and_the_resume_behaviour(self):
        root = make_book(self.ws, "tiny")
        seed_done(root, "cleanup")
        self.ws.update("translation", provider="etranslation")
        self.ctx.reload_config()
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            await self.select(pilot, screen, "s1b.translate")
            await pilot.click("#run-task")
            await settle(pilot)
            self.assertEqual(app.screen.state, "confirm")
            text = app.screen.query_one("#run-confirm", Static).render().plain
            self.assertIn("Provider: etranslation", text)
            self.assertIn("up to two hours", text)
            self.assertIn("running it again resumes the request already in flight", text)
            await pilot.click("#run-close")


if __name__ == "__main__":
    unittest.main()
