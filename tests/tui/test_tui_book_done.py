"""Marking a book as done, in the UI: finished books sit at the end of the home list with a green
[x], and the book screen marks and reopens through the `book done` / `book reopen` commands.
The state helper needs no Textual; the screen tests skip without it.
"""
import logging
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402
from harness import EXAMPLE_LAUNCHER, GuardedCase, Workspace, make_book, root_group  # noqa: E402
from lib import books, manifest, orchestrator  # noqa: E402
from tui.services import state  # noqa: E402
from tui.services.context import Context  # noqa: E402

logging.getLogger("asyncio").setLevel(logging.ERROR)


def seed(ws):
    """Four books. `late` and `early` are done; `new` and `old` are in progress. By activity, newest first:
    late (Oct 20), new (Oct 15), early (Oct 10), old (Oct 1)."""
    stamps = {"old": "2026-10-01", "early": "2026-10-10", "new": "2026-10-15", "late": "2026-10-20"}
    for slug, day in stamps.items():
        root = make_book(ws, slug, source=False)
        manifest.record_task(root, "s1b.cleanup", status="done", output="x", completed_at=f"{day}T10:00:00+00:00")
    books.mark_done(ws.book("late"), on=date(2026, 10, 21))
    books.mark_done(ws.book("early"), on=date(2026, 10, 11))


class StateTests(GuardedCase):
    def test_the_done_mark_is_plain_ascii_so_every_terminal_font_can_draw_it(self):
        self.assertTrue(state.DONE_MARK.isascii())
        self.assertEqual(state.DONE_MARK, "[x]")

    def test_done_books_come_last_and_each_group_is_newest_first(self):
        with Workspace() as ws:
            seed(ws)
            rows = state.list_books(Context.load(ws.config_path))
            self.assertEqual([r.slug for r in rows], ["new", "old", "late", "early"])
            self.assertEqual([r.done_on for r in rows], [None, None, "2026-10-21", "2026-10-11"])

    def test_the_task_count_called_done_is_not_the_done_mark(self):
        with Workspace() as ws:
            seed(ws)
            row = next(r for r in state.list_books(Context.load(ws.config_path)) if r.slug == "new")
            self.assertEqual((row.done, row.done_on), (1, None))    # one task done, the book is not

    def test_reopening_puts_a_book_back_among_those_in_progress(self):
        with Workspace() as ws:
            seed(ws)
            books.reopen(ws.book("late"))
            self.assertEqual([r.slug for r in state.list_books(Context.load(ws.config_path))], ["late", "new", "old", "early"])


try:
    import textual  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Textual is not installed (uv sync --extra tui)")

from textual.widgets import DataTable, Static  # noqa: E402
from tui.app import PipelineApp  # noqa: E402
from tui.screens.book import BookScreen  # noqa: E402
from tui.screens.run import RunScreen  # noqa: E402
from tui.widgets.run_button import RunButton  # noqa: E402

SIZE = (150, 50)


async def settle(pilot, times=3):
    for _ in range(times):
        await pilot.pause()


class ScreenCase(GuardedCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)

    def app(self):
        return PipelineApp(self.ctx, root_group())


class HomeListTests(ScreenCase):
    async def test_finished_books_are_last_with_a_green_mark_and_the_rest_are_unmarked(self):
        seed(self.ws)
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            table = app.screen.query_one(DataTable)
            cells = [table.get_row_at(i) for i in range(table.row_count)]
            self.assertEqual([c[0].plain if hasattr(c[0], "plain") else str(c[0]) for c in cells],
                             ["new", "old", "[x] late", "[x] early"])
            mark = cells[2][0]
            self.assertTrue(any("green" in str(span.style) for span in mark.spans), mark.spans)
            self.assertEqual(str(cells[2][5]), "done 2026-10-21")        # the last-activity column says when
            self.assertEqual(str(cells[0][5]), "2026-10-15 10:00")      # books in progress show their activity
            self.assertNotIsInstance(cells[0][0], type(mark))           # plain text, no mark

    async def test_a_finished_book_still_opens_by_its_name(self):
        seed(self.ws)
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=table.get_row_index("late"))
            await settle(pilot)
            await pilot.press("enter")
            await settle(pilot)
            self.assertIsInstance(app.screen, BookScreen)
            self.assertEqual(app.screen.slug, "late")

    async def test_with_nothing_marked_the_list_is_unchanged(self):
        make_book(self.ws, "only", source=False)
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            table = app.screen.query_one(DataTable)
            self.assertEqual(str(table.get_row_at(0)[0]), "only")


class BookScreenTests(ScreenCase):
    async def open_book(self, pilot, app, slug):
        app.open_book(slug)
        await settle(pilot)
        self.assertIsInstance(app.screen, BookScreen)
        return app.screen

    def title(self, screen):
        return screen.query_one("#book-title", Static).render().plain

    async def test_the_title_and_button_follow_the_mark(self):
        seed(self.ws)
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app, "new")
            self.assertTrue(self.title(screen).startswith("new   ISBN"))
            self.assertEqual(str(screen.query_one("#toggle-done", RunButton).label), "Mark as done")
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app, "late")
            self.assertTrue(self.title(screen).startswith("[x] late (done 2026-10-21)"), self.title(screen))
            self.assertEqual(str(screen.query_one("#toggle-done", RunButton).label), "Reopen")

    async def test_marking_a_book_with_unfinished_tasks_asks_first_then_marks_it(self):
        seed(self.ws)
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app, "new")
            await pilot.click("#toggle-done")
            await settle(pilot)
            self.assertIsInstance(app.screen, RunScreen)
            self.assertEqual(app.screen.state, "confirm")
            text = app.screen.query_one("#run-confirm", Static).render().plain
            self.assertIn("12 of this book's 13 tasks are not done", text)
            self.assertIn("nothing is deleted and you can reopen it", text)
            self.assertTrue(app.screen.query_one("#run-hint").text.rstrip().endswith("book done new"))
            await pilot.click("#run-go")
            await settle(pilot)
            await app.session.wait()
            await settle(pilot, 5)
            await pilot.press("escape")
            await settle(pilot)

            self.assertEqual(books.done_on(self.ws.book("new")), date.today().isoformat())
            self.assertIn(f"[x] new (done {date.today().isoformat()})", self.title(screen))
            self.assertEqual(str(screen.query_one("#toggle-done", RunButton).label), "Reopen")

    async def test_reopening_needs_no_question_and_takes_the_mark_off(self):
        seed(self.ws)
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app, "late")
            await pilot.click("#toggle-done")
            await settle(pilot)
            self.assertIsInstance(app.screen, RunScreen)
            self.assertNotEqual(app.screen.state, "confirm")        # it just runs
            self.assertTrue(app.session.request.argv[-3:] == ["book", "reopen", "late"])
            await app.session.wait()
            await settle(pilot, 5)
            await pilot.press("escape")
            await settle(pilot)
            self.assertIsNone(books.done_on(self.ws.book("late")))
            self.assertTrue(self.title(screen).startswith("late   ISBN"))
            self.assertEqual(str(screen.query_one("#toggle-done", RunButton).label), "Mark as done")

            await pilot.press("escape")                              # home: it is back among the books in progress
            await settle(pilot)
            table = app.screen.query_one(DataTable)
            self.assertEqual([str(table.get_row_at(i)[0]) for i in range(table.row_count)][0], "late")

    async def test_a_book_with_every_task_done_is_marked_without_a_question(self):
        with harness.example_manifests():
            root = make_book(self.ws, "full", source=False)
            for node in orchestrator._load_graph():
                manifest.record_task(root, f"{node['system']}.{node['task']['name']}", status="done", output="x",
                                     completed_at="2026-10-01T00:00:00+00:00")
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await self.open_book(pilot, app, "full")
            await pilot.click("#toggle-done")
            await settle(pilot)
            self.assertNotEqual(app.screen.state, "confirm")
            await app.session.wait()
            self.assertEqual(books.done_on(root), date.today().isoformat())

    async def test_the_button_is_disabled_while_another_command_runs(self):
        seed(self.ws)
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_book(pilot, app, "new")
            self.assertFalse(screen.query_one("#toggle-done", RunButton).disabled)
            app.busy = True       # what the app sets for the whole life of a run
            await settle(pilot)
            self.assertTrue(screen.query_one("#toggle-done", RunButton).disabled)
            app.busy = False


if __name__ == "__main__":
    unittest.main()
