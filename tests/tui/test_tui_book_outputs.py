"""ADR 023, T2.5 — outputs, the book folder, manifest.yaml and the ISBN.

The ISBN rule needs no Textual; the screen tests skip without it.
"""
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402
from harness import EXAMPLE_LAUNCHER, FIXTURE_ISBN, GuardedCase, Workspace, make_book, root_group  # noqa: E402
from lib import manifest  # noqa: E402
from tui.services.context import Context  # noqa: E402
from tui.services.validate import normalize_isbn  # noqa: E402

logging.getLogger("asyncio").setLevel(logging.ERROR)


class IsbnRuleTests(unittest.TestCase):
    def test_valid_forms_are_normalised_to_plain_digits(self):
        for typed, stored in (("9781234567897", "9781234567897"), ("978-1-2345-6789-7", "9781234567897"),
                              (" 978 1234567897 ", "9781234567897"), ("0306406152", "0306406152"),
                              ("080442957x", "080442957X"), ("0-8044-2957-X", "080442957X")):
            self.assertEqual(normalize_isbn(typed), (stored, None), typed)

    def test_everything_else_is_rejected(self):
        for typed in ("", "   ", "12345", "97812345678", "97812345678977", "978123456789X", "abcdefghij", "0306406152X"):
            isbn, problem = normalize_isbn(typed)
            self.assertIsNone(isbn, typed)
            self.assertTrue(problem, typed)


try:
    import textual  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Textual is not installed (uv sync --extra tui)")

from textual.widgets import Button, DataTable, Input, Static  # noqa: E402
from tui.app import PipelineApp  # noqa: E402
from tui.screens.home import HomeScreen  # noqa: E402
from tui.screens.run import RunScreen  # noqa: E402

SIZE = (150, 50)


async def settle(pilot, times=3):
    for _ in range(times):
        await pilot.pause()


class OutputsCase(GuardedCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)
        self.root = make_book(self.ws, "tiny")
        cleaned = self.root / "s1b" / "cleaned" / "tiny_source.txt"
        cleaned.parent.mkdir(parents=True)
        cleaned.write_text("cleaned", encoding="utf-8")
        manifest.record_task(self.root, "s1b.cleanup", status="done", output="s1b/cleaned/tiny_source.txt",
                             completed_at="2026-10-01T10:00:00+00:00", output_mtime=cleaned.stat().st_mtime)
        manifest.record_task(self.root, "s1b.translate", status="stale", invalidated_by="s1b.cleanup",
                             output="s1b/translated/es/tiny_source.txt")  # recorded, but the file is not on disk
        manifest.record_task(self.root, "s1b.ortho", status="failed", error="boom", attempted_at="2026-10-01T00:00:00+00:00")

    def app(self):
        return PipelineApp(self.ctx, root_group())

    async def open_book(self, pilot, app):
        app.open_book("tiny")
        await settle(pilot)
        return app.screen

    async def select(self, pilot, screen, key):
        table = screen.query_one(DataTable)
        table.move_cursor(row=table.get_row_index(key))
        await settle(pilot)


class OutputsTests(OutputsCase):
    async def test_open_file_and_open_folder_target_the_recorded_output(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            await self.select(pilot, screen, "s1b.cleanup")
            with mock.patch("tui.screens.book.open_path") as opened:
                await pilot.click("#open-file")
                await pilot.click("#open-folder")
                await settle(pilot)
            self.assertEqual([c.args[0] for c in opened.call_args_list],
                             [self.root / "s1b" / "cleaned" / "tiny_source.txt", self.root / "s1b" / "cleaned"])

    async def test_actions_are_disabled_for_rows_without_an_output_on_disk(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            for key, enabled in (("s1b.cleanup", True), ("s1b.translate", False),   # stale, file missing
                                 ("s1b.ortho", False),                               # failed: no output
                                 ("s1b.copyedit", False),                            # blocked
                                 ("s1d.synopsis", False)):
                await self.select(pilot, screen, key)
                self.assertEqual(not screen.query_one("#open-file", Button).disabled, enabled, key)
                self.assertEqual(not screen.query_one("#open-folder", Button).disabled, enabled, key)
            screen.query_one(DataTable).move_cursor(row=0)  # a system heading
            await settle(pilot)
            self.assertTrue(screen.query_one("#open-file", Button).disabled)

    async def test_the_book_folder_and_manifest_open_externally(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_book(pilot, app)
            before = (self.root / "manifest.yaml").read_bytes()
            with mock.patch("tui.screens.book.open_path") as opened:
                await pilot.click("#open-book-folder")
                await pilot.click("#open-manifest")
                await settle(pilot)
            self.assertEqual([c.args[0] for c in opened.call_args_list], [self.root, self.root / "manifest.yaml"])
            self.assertEqual((self.root / "manifest.yaml").read_bytes(), before)  # opening is not writing


class IsbnTests(OutputsCase):
    async def set_isbn(self, pilot, app, screen, text):
        screen.query_one("#isbn", Input).value = text
        await settle(pilot)
        await pilot.click("#set-isbn")
        await settle(pilot)

    async def test_an_invalid_isbn_is_rejected_without_running_anything(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            for typed in ("", "12345", "97812345678977"):
                await self.set_isbn(pilot, app, screen, typed)
                self.assertIs(app.screen, screen)
                self.assertTrue(screen.query_one("#isbn-error", Static).render().plain, typed)
            self.assertIsNone(app.session)
            self.assertNotIn("isbn", manifest.load(self.root))

    async def test_setting_an_isbn_builds_the_right_command_and_the_book_list_shows_it(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            with mock.patch.object(app, "begin_run") as begin:
                await self.set_isbn(pilot, app, screen, "978-1-2345-6789-7")
            request = begin.call_args.args[0]
            self.assertEqual(request.argv[-4:], ["book", "set-isbn", "tiny", FIXTURE_ISBN])  # hyphens dropped
            self.assertEqual(request.warnings, [])  # none set yet: no confirmation

            await self.set_isbn(pilot, app, screen, "978-1-2345-6789-7")
            self.assertIsInstance(app.screen, RunScreen)
            self.assertEqual(app.screen.state, "running" if app.session.running else "done")
            await app.session.wait()
            await settle(pilot, 5)
            self.assertEqual(manifest.load(self.root)["isbn"], FIXTURE_ISBN)
            await pilot.press("escape")
            await settle(pilot)
            self.assertIn(FIXTURE_ISBN, screen.query_one("#book-title", Static).render().plain)  # the book screen refreshed
            await pilot.press("escape")
            await settle(pilot)
            self.assertIsInstance(app.screen, HomeScreen)
            table = app.screen.query_one(DataTable)
            self.assertEqual(str(table.get_row("tiny")[4]), FIXTURE_ISBN)

    async def test_replacing_an_existing_isbn_asks_first(self):
        manifest.update(self.root, isbn="9780000000002")
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app)
            await self.set_isbn(pilot, app, screen, FIXTURE_ISBN)
            self.assertIsInstance(app.screen, RunScreen)
            self.assertEqual(app.screen.state, "confirm")
            text = app.screen.query_one("#run-confirm", Static).render().plain
            self.assertIn("already has the ISBN 9780000000002", text)
            self.assertIn(f"Setting {FIXTURE_ISBN} replaces it", text)
            await pilot.click("#run-close")  # decline
            await settle(pilot)
            self.assertEqual(manifest.load(self.root)["isbn"], "9780000000002")
            self.assertIsNone(app.session)

            await self.set_isbn(pilot, app, screen, FIXTURE_ISBN)
            await pilot.click("#run-go")
            await settle(pilot)
            await app.session.wait()
            self.assertEqual(manifest.load(self.root)["isbn"], FIXTURE_ISBN)


if __name__ == "__main__":
    unittest.main()
