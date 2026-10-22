"""ADR 023, T4.1 — the dashboard view. Every displayed figure is compared with the
real `s3 dashboard` output for the same ledgers. The service needs no Textual; the
screen tests skip without it.
"""
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402
from harness import EXAMPLE_LAUNCHER, GuardedCase, Workspace, root_group, run_cli, seed_portfolio  # noqa: E402
from lib import formatting, manifest  # noqa: E402
from tui.services import dashboard_view  # noqa: E402
from tui.services.context import Context  # noqa: E402

logging.getLogger("asyncio").setLevel(logging.ERROR)


def cli_lines(ws, *args):
    code, out = run_cli(ws, "s3", "dashboard", *args, example=True)
    assert code == 0, out
    return out.replace("\r\n", "\n").split("\n")


def line_for(lines, start):
    return next(line for line in lines if line.startswith(start))


class PortfolioViewTests(GuardedCase):
    def test_every_figure_is_in_the_cli_line_for_that_book(self):
        with Workspace() as ws:
            slugs = seed_portfolio(ws)
            view = dashboard_view.portfolio_view(Context.load(ws.config_path))
            lines = cli_lines(ws)
            self.assertEqual(view.heading, line_for(lines, "Portfolio:"))
            self.assertEqual([r.slug for r in view.rows], slugs)
            for row in view.rows:
                cli = line_for(lines, row.slug)
                for cell in (row.tasks, row.cost, row.compute, row.revenue, row.margin, row.span):
                    self.assertIn(cell, cli, f"{row.slug}: {cell!r}")
            self.assertEqual(view.totals, line_for(lines, "Totals:"))

    def test_the_outlier_book_is_marked_and_summarised_like_the_cli(self):
        with Workspace() as ws:
            seed_portfolio(ws)
            view = dashboard_view.portfolio_view(Context.load(ws.config_path))
            rows = {r.slug: r for r in view.rows}
            self.assertEqual((rows["book-d"].cost, rows["book-d"].flagged), ("$0.100 !", True))
            self.assertEqual((rows["book-a"].cost, rows["book-a"].flagged), ("$0.010", False))
            flagged_lines = [l for l in cli_lines(ws) if l.startswith("Flagged:")]
            self.assertEqual(view.flags, flagged_lines)
            self.assertEqual(view.flags, ["Flagged: book-d — API cost $0.100 is 10.0x the portfolio average ($0.010)"])

    def test_revenue_by_currency_and_the_api_margin(self):
        with Workspace() as ws:
            seed_portfolio(ws)
            rows = {r.slug: r for r in dashboard_view.portfolio_view(Context.load(ws.config_path)).rows}
            self.assertEqual((rows["book-d"].revenue, rows["book-d"].margin), ("48.60 USD", "$48.50"))
            self.assertEqual((rows["book-c"].revenue, rows["book-c"].margin), ("20.00 EUR", "-"))  # no USD, so no margin
            self.assertEqual((rows["book-a"].revenue, rows["book-a"].margin), ("-", "-"))

    def test_no_books_is_an_empty_view(self):
        with Workspace() as ws:
            view = dashboard_view.portfolio_view(Context.load(ws.config_path))
            self.assertEqual((view.rows, view.totals, view.flags), ([], "", []))


class BookViewTests(GuardedCase):
    def test_every_figure_is_in_the_cli_line_for_that_task(self):
        with Workspace() as ws:
            seed_portfolio(ws)
            view = dashboard_view.book_view(Context.load(ws.config_path), "book-d")
            lines = cli_lines(ws, "book-d")
            self.assertEqual(len(view.tasks), 2)
            for task in view.tasks:
                cli = line_for(lines, task.label)
                for cell in (task.duration, task.provider_model, task.usage, task.cost):
                    self.assertIn(cell, cli, f"{task.label}: {cell!r}")
            self.assertEqual(view.total, line_for(lines, "Total:"))
            self.assertEqual(view.span, line_for(lines, "In-pipeline span:"))
            self.assertEqual(view.revenue, line_for(lines, "Revenue:"))
            self.assertEqual(view.margin, line_for(lines, "Reported margin"))
            self.assertEqual(view.flags, [l for l in lines if l.startswith("Flagged:")])

    def test_the_slow_task_is_marked_and_its_line_matches_the_cli(self):
        with Workspace() as ws:
            seed_portfolio(ws)
            view = dashboard_view.book_view(Context.load(ws.config_path), "book-d")
            cleanup = next(t for t in view.tasks if t.label == "s1b.cleanup")
            self.assertEqual((cleanup.duration, cleanup.flagged), ("1m 40s !", True))
            translate = next(t for t in view.tasks if t.label == "s1b.translate")
            self.assertEqual((translate.duration, translate.cost, translate.usage), ("5s", "$0.000100", "5,000 characters"))
            self.assertEqual(view.flags, ["Flagged: s1b.cleanup — 1m 40s is 10.0x the portfolio average for this task (10s)"])

    def test_a_book_without_flags_or_revenue_has_neither_line(self):
        with Workspace() as ws:
            seed_portfolio(ws)
            view = dashboard_view.book_view(Context.load(ws.config_path), "book-a")
            self.assertEqual((view.flags, view.revenue, view.margin), ([], None, None))

    def test_a_task_with_no_cost_shows_a_dash_and_a_book_with_none_gets_the_omission_note(self):
        with Workspace() as ws:
            seed_portfolio(ws)
            view = dashboard_view.book_view(Context.load(ws.config_path), "book-b")
            fmt = next(t for t in view.tasks if t.label == "s1b.format")
            self.assertEqual((fmt.cost, fmt.provider_model, fmt.usage), ("-", "-", "-"))

            # a book where nothing has a cost at all
            root = ws.book("book-free")
            (root / "s1b" / "source").mkdir(parents=True)
            manifest.save(root, {"slug": "book-free"})
            manifest.record_task(root, "s1b.format", status="done", output="s1b/formatted/x.odt",
                                 completed_at="2026-09-09T10:00:00+00:00", duration_seconds=2.0)
            view = dashboard_view.book_view(Context.load(ws.config_path), "book-free")
            self.assertTrue(view.total.endswith(formatting.COST_OMITTED_NOTE), view.total)
            self.assertIn("-", [t.cost for t in view.tasks])
            self.assertEqual(view.total, line_for(cli_lines(ws, "book-free"), "Total:"))

    def test_a_stale_task_is_labelled_stale_but_still_counted(self):
        with Workspace() as ws:
            seed_portfolio(ws)
            manifest.mark_stale(ws.book("book-a"), "s1b.translate", invalidated_by="s1b.cleanup")
            view = dashboard_view.book_view(Context.load(ws.config_path), "book-a")
            self.assertIn("s1b.translate (stale)", [t.label for t in view.tasks])
            self.assertEqual(view.total, line_for(cli_lines(ws, "book-a"), "Total:"))


try:
    import textual  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Textual is not installed (uv sync --extra tui)")

from textual.widgets import DataTable, Static  # noqa: E402
from tui.app import PipelineApp  # noqa: E402
from tui.screens.performance import PerformanceScreen  # noqa: E402

SIZE = (170, 60)


async def settle(pilot, times=3):
    for _ in range(times):
        await pilot.pause()


class DashboardScreenTests(GuardedCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)

    async def open(self, pilot, app):
        await pilot.press("d")
        await settle(pilot)
        self.assertIsInstance(app.screen, PerformanceScreen)
        return app.screen

    def cells(self, table, key):
        return [str(c) for c in table.get_row(key)]

    async def test_the_portfolio_table_shows_the_service_rows_and_the_cli_wording(self):
        seed_portfolio(self.ws)
        lines = cli_lines(self.ws)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            table = screen.query_one("#portfolio", DataTable)
            self.assertEqual([str(c.label) for c in table.columns.values()],
                             ["Book", "Tasks done", "API cost", "Compute time", "Revenue", "Margin (API)", "In-pipeline span"])
            for row in screen.portfolio.rows:
                self.assertEqual(self.cells(table, row.slug),
                                 [row.slug, row.tasks, row.cost, row.compute, row.revenue, row.margin, row.span])
                for cell in self.cells(table, row.slug):
                    self.assertIn(cell, line_for(lines, row.slug))
            self.assertEqual(self.cells(table, "book-d")[2], "$0.100 !")
            self.assertEqual(screen.query_one("#portfolio-title", Static).render().plain, line_for(lines, "Portfolio:"))
            self.assertEqual(screen.query_one("#portfolio-totals", Static).render().plain, line_for(lines, "Totals:"))
            self.assertEqual(screen.query_one("#portfolio-flags", Static).render().plain,
                             line_for(lines, "Flagged: book-d"))

    async def test_the_margin_note_explains_what_it_excludes(self):
        seed_portfolio(self.ws)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            note = screen.query_one("#margin-note", Static).render().plain
            self.assertIn("Margin (API)", note)
            self.assertIn("revenue minus tracked API cost only", note)
            self.assertIn("design, freelance editorial time, ISBN fees and print cost", note)

    async def test_choosing_a_book_shows_its_tasks_and_summary(self):
        seed_portfolio(self.ws)
        lines = cli_lines(self.ws, "book-d")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            portfolio = screen.query_one("#portfolio", DataTable)
            portfolio.move_cursor(row=portfolio.get_row_index("book-d"))
            await settle(pilot)
            tasks = screen.query_one("#book-tasks", DataTable)
            self.assertEqual(tasks.row_count, 2)
            self.assertEqual(screen.query_one("#book-title", Static).render().plain, "Book: book-d")
            first = [str(c) for c in tasks.get_row_at(0)]
            self.assertEqual(first[0], "s1b.cleanup")
            self.assertEqual(first[1], "1m 40s !")
            summary = screen.query_one("#book-summary", Static).render().plain.split("\n")
            self.assertEqual(summary, [line_for(lines, "Total:"), line_for(lines, "In-pipeline span:"),
                                       line_for(lines, "Revenue:"), line_for(lines, "Reported margin"),
                                       line_for(lines, "Flagged:")])

    async def test_no_books_says_so(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            self.assertIn("No books found", screen.query_one("#portfolio-title", Static).render().plain)
            self.assertEqual(screen.query_one("#portfolio", DataTable).row_count, 0)

    async def test_the_dashboard_refreshes_when_the_screen_is_shown_again(self):
        seed_portfolio(self.ws)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            before = self.cells(screen.query_one("#portfolio", DataTable), "book-a")[1]
            manifest.record_task(self.ws.book("book-a"), "s1b.ortho", status="done", output="s1b/ortho/x.txt",
                                 completed_at="2026-09-20T10:00:00+00:00", duration_seconds=3.0)
            screen.refresh_dashboard()
            await settle(pilot)
            after = self.cells(screen.query_one("#portfolio", DataTable), "book-a")[1]
            self.assertEqual((before, after), ("2/13", "3/13"))


if __name__ == "__main__":
    unittest.main()
