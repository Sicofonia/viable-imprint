"""ADR 023, T4.2 — sales ingest from the performance screen. The receipt parser needs
no Textual; the screen tests skip without it.
"""
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402
from harness import EXAMPLE_LAUNCHER, FIXTURE_ISBN, GuardedCase, Workspace, fixture, make_book, root_group, run_cli  # noqa: E402
from lib import manifest  # noqa: E402
from tui.services.context import Context  # noqa: E402
from tui.services.output import ingest_receipt  # noqa: E402

logging.getLogger("asyncio").setLevel(logging.ERROR)


class ReceiptParserTests(unittest.TestCase):
    def test_a_normal_ingest(self):
        lines = ["  Format: kdp", "  Rows matching ISBN 9781234567897: 1", "  New entries recorded: 1",
                 "Saved: C:/x/s3/sales-ingest/2026-10-22/combined.txt"]
        receipt = ingest_receipt(lines)
        self.assertEqual((receipt.format, receipt.matched, receipt.added, receipt.skipped), ("kdp", 1, 1, 0))
        self.assertEqual(receipt.sentence(), "kdp: 1 row matched this book's ISBN, 1 entry added, 0 skipped as duplicates.")

    def test_a_repeat_reports_the_skipped_duplicates(self):
        lines = ["  Format: ingramspark", "  Rows matching ISBN 9781234567897: 1", "  New entries recorded: 0",
                 "  Already recorded (skipped as duplicates): 1"]
        receipt = ingest_receipt(lines)
        self.assertEqual((receipt.added, receipt.skipped), (0, 1))
        self.assertIn("0 entries added, 1 skipped as duplicates", receipt.sentence())

    def test_an_error_is_passed_through_in_the_clis_words(self):
        lines = ["Error: Could not recognize this CSV's format automatically (columns: colour, shape).",
                 "Pass --format explicitly. Known formats: ingramspark, kdp"]
        receipt = ingest_receipt(lines)
        self.assertIsNone(receipt.format)
        self.assertEqual(receipt.error, "\n".join(lines))
        self.assertEqual(receipt.sentence(), receipt.error)

    def test_no_receipt_for_unrelated_output(self):
        self.assertIsNone(ingest_receipt(["Created: books/x/"]))
        self.assertIsNone(ingest_receipt([]))


try:
    import textual  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Textual is not installed (uv sync --extra tui)")

from textual.widgets import Button, DataTable, Select, Static  # noqa: E402
from tui.app import PipelineApp  # noqa: E402
from tui.screens.book import BookScreen  # noqa: E402
from tui.screens.performance import PerformanceScreen  # noqa: E402
from tui.screens.run import RunScreen  # noqa: E402
from tui.widgets.run_button import RunButton  # noqa: E402

SIZE = (170, 70)


async def settle(pilot, times=3):
    for _ in range(times):
        await pilot.pause()


class SalesScreenTests(GuardedCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)

    def book(self, slug, isbn=FIXTURE_ISBN):
        root = make_book(self.ws, slug, source=False)
        if isbn:
            manifest.update(root, isbn=isbn)
        return root

    async def open(self, pilot, app):
        await pilot.press("d")
        await settle(pilot)
        self.assertIsInstance(app.screen, PerformanceScreen)
        return app.screen

    async def choose(self, pilot, screen, slug, csv, format_=None):
        screen.query_one("#ingest-book", Select).value = slug
        screen.query_one("#ingest-csv").value = str(csv)
        if format_:
            screen.query_one("#ingest-format", Select).value = format_
        await settle(pilot)

    async def ingest(self, pilot, app, screen):
        await pilot.click("#run-ingest")
        await settle(pilot)
        self.assertIsInstance(app.screen, RunScreen)  # no provider call: it started at once
        await app.session.wait()
        await settle(pilot, 5)
        await pilot.press("escape")
        await settle(pilot)

    def receipt(self, screen):
        return screen.query_one("#ingest-receipt", Static).render().plain

    async def test_both_csv_fixtures_ingest_through_the_ui(self):
        ing, kdp = self.book("ing"), self.book("kdp")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await self.choose(pilot, screen, "ing", fixture("tiny_ingramspark.csv"))
            await self.ingest(pilot, app, screen)
            self.assertEqual(manifest.load(ing)["sales"][0]["revenue"], 48.6)
            self.assertIn("ingramspark: 1 row matched", self.receipt(screen))
            self.assertIn("1 entry added", self.receipt(screen))

            await self.choose(pilot, screen, "kdp", fixture("tiny_kdp.csv"))
            await self.ingest(pilot, app, screen)
            sale = manifest.load(kdp)["sales"][0]
            self.assertEqual((sale["units"], sale["revenue"]), (5, 13.0))
            self.assertIn("kdp:", self.receipt(screen))

    async def test_the_dashboard_refreshes_with_the_new_revenue(self):
        self.book("ing")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            table = screen.query_one("#portfolio", DataTable)
            self.assertEqual(str(table.get_row("ing")[4]), "-")
            await self.choose(pilot, screen, "ing", fixture("tiny_ingramspark.csv"))
            await self.ingest(pilot, app, screen)
            self.assertEqual(str(table.get_row("ing")[4]), "48.60 USD")

    async def test_a_second_ingest_of_the_same_file_adds_nothing(self):
        root = self.book("ing")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await self.choose(pilot, screen, "ing", fixture("tiny_ingramspark.csv"))
            await self.ingest(pilot, app, screen)
            await self.ingest(pilot, app, screen)
            self.assertIn("0 entries added, 1 skipped as duplicates", self.receipt(screen))
            self.assertEqual(len(manifest.load(root)["sales"]), 1)

    async def test_a_book_without_an_isbn_cannot_start_an_ingest(self):
        self.book("no-isbn", isbn=None)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await self.choose(pilot, screen, "no-isbn", fixture("tiny_kdp.csv"))
            run = screen.query_one("#run-ingest", RunButton)
            self.assertTrue(run.disabled)
            self.assertEqual(run.tooltip, "Set this book's ISBN first")
            first = screen.query_one("#set-isbn-first", Button)
            self.assertTrue(first.display)
            await pilot.click("#set-isbn-first")
            await settle(pilot)
            self.assertIsInstance(app.screen, BookScreen)  # where the ISBN is entered
            self.assertEqual(app.screen.slug, "no-isbn")

    async def test_the_set_isbn_shortcut_is_hidden_when_the_book_has_one(self):
        self.book("has-isbn")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await self.choose(pilot, screen, "has-isbn", fixture("tiny_kdp.csv"))
            self.assertFalse(screen.query_one("#set-isbn-first", Button).display)
            self.assertFalse(screen.query_one("#run-ingest", RunButton).disabled)

    async def test_it_needs_a_book_and_a_file_before_it_can_run(self):
        self.book("ing")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            screen.query_one("#ingest-csv").value = ""
            await settle(pilot)
            self.assertTrue(screen.query_one("#run-ingest", RunButton).disabled)
            self.assertEqual(screen.query_one("#run-ingest", RunButton).tooltip, "Choose the CSV file")
            screen.query_one("#ingest-csv").value = str(self.ws.base / "missing.csv")
            await settle(pilot)
            self.assertEqual(screen.query_one("#run-ingest", RunButton).tooltip, "That file does not exist")

    async def test_an_unrecognised_csv_shows_the_clis_error_and_the_format_choice(self):
        self.book("ing")
        odd = self.ws.base / "odd.csv"
        odd.write_text("colour,shape\nred,round\n", encoding="utf-8")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await self.choose(pilot, screen, "ing", odd)
            await self.ingest(pilot, app, screen)
            text = self.receipt(screen)
            self.assertIn("Could not recognize this CSV's format automatically", text)
            self.assertIn("Known formats: ingramspark, kdp", text)
            select = screen.query_one("#ingest-format", Select)
            self.assertEqual({str(label) for label, _ in select._options}, {"Auto-detect", "ingramspark", "kdp"})

    async def test_a_forced_format_reaches_the_command(self):
        self.book("kdp")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await self.choose(pilot, screen, "kdp", fixture("tiny_kdp.csv"))
            self.assertNotIn("--format", screen.query_one("#ingest-hint").text)
            screen.query_one("#ingest-format", Select).value = "kdp"
            await settle(pilot)
            self.assertTrue(screen.query_one("#ingest-hint").text.rstrip().endswith("--format kdp"))
            await self.ingest(pilot, app, screen)
            self.assertEqual(app.session.request.argv[-2:], ["--format", "kdp"])
            self.assertIn("kdp:", self.receipt(screen))


if __name__ == "__main__":
    unittest.main()
