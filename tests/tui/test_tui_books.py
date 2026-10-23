"""ADR 023, T2.1 — the books list and the new-book flow.

The state and file helpers need no Textual and always run; the screen tests skip
without it.
"""
import logging
import os
import shutil
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402
from harness import EXAMPLE_LAUNCHER, GuardedCase, Workspace, fixture, make_book, root_group  # noqa: E402
from lib import manifest  # noqa: E402
from tui.services import files, state  # noqa: E402
from tui.services.context import Context  # noqa: E402

logging.getLogger("asyncio").setLevel(logging.ERROR)

TOTAL = 13   # book-scoped tasks: 5 in s1b + 8 in s1d


def seed(ws):
    """Four books in the states the list must tell apart, newest activity first:
    failure (Oct 5), stale (Oct 4), partial (Oct 3), fresh (Oct 1, from its manifest's mtime)."""
    for slug in ("fresh", "partial", "failure", "stale"):
        make_book(ws, slug, source=False)
    manifest.update(ws.book("partial"), isbn=harness.FIXTURE_ISBN)
    done = lambda name, stamp: dict(status="done", output=f"s1b/{name}/x.txt", completed_at=stamp, duration_seconds=1.0)
    manifest.record_task(ws.book("partial"), "s1b.cleanup", **done("cleaned", "2026-10-02T10:00:00+00:00"))
    manifest.record_task(ws.book("partial"), "s1b.translate", **done("translated", "2026-10-03T10:00:00+00:00"))
    manifest.record_task(ws.book("failure"), "s1b.cleanup", status="failed", error="boom",
                         attempted_at="2026-10-05T09:00:00+00:00", duration_seconds=1.0)
    manifest.record_task(ws.book("stale"), "s1b.cleanup", **done("cleaned", "2026-10-04T08:00:00+00:00"))
    manifest.record_task(ws.book("stale"), "s1b.translate", status="stale", invalidated_by="s1b.cleanup",
                         output="s1b/translated/es/x.txt", completed_at="2026-10-04T07:00:00+00:00")
    stamp = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
    os.utime(ws.book("fresh") / "manifest.yaml", (stamp, stamp))


class BookListTests(GuardedCase):
    def test_counts_order_and_isbn_for_books_in_four_states(self):
        with Workspace() as ws:
            seed(ws)
            rows = state.list_books(Context.load(ws.config_path))
            self.assertEqual([r.slug for r in rows], ["failure", "stale", "partial", "fresh"])
            by_slug = {r.slug: r for r in rows}
            self.assertEqual([(r.done, r.failed, r.stale, r.tasks_total) for r in (
                by_slug["fresh"], by_slug["partial"], by_slug["failure"], by_slug["stale"])],
                [(0, 0, 0, TOTAL), (2, 0, 0, TOTAL), (0, 1, 0, TOTAL), (1, 0, 1, TOTAL)])
            self.assertEqual(by_slug["partial"].isbn, harness.FIXTURE_ISBN)
            self.assertIsNone(by_slug["fresh"].isbn)
            self.assertEqual(by_slug["failure"].last_activity, "2026-10-05T09:00:00+00:00")
            self.assertEqual(state.format_activity(by_slug["failure"].last_activity), "2026-10-05 09:00")
            self.assertTrue(by_slug["fresh"].last_activity.startswith("2026-10-01"))

    def test_counts_equal_the_s2_status_rows(self):
        with Workspace() as ws:
            seed(ws)
            from lib import orchestrator
            for row in state.list_books(Context.load(ws.config_path)):
                statuses = [r["status"] for r in orchestrator.book_status(row.path)]
                self.assertEqual((row.done, row.failed, row.stale),
                                 (statuses.count("done"), statuses.count("failed"), statuses.count("stale")))

    def test_folders_without_a_manifest_and_a_missing_books_dir_are_ignored(self):
        with Workspace() as ws:
            ctx = Context.load(ws.config_path)
            self.assertEqual(state.list_books(ctx), [])
            (ws.books_dir / "stray-folder").mkdir()
            (ws.books_dir / "a-file.txt").write_text("x", encoding="utf-8")
            self.assertEqual(state.list_books(ctx), [])
            shutil.rmtree(ws.books_dir)
            self.assertEqual(state.list_books(ctx), [])


class NewBookHelperTests(GuardedCase):
    def test_slug_rules(self):
        with Workspace() as ws:
            ctx = Context.load(ws.config_path)
            self.assertIsNone(files.slug_problem(ctx, "life-as-explorer"))
            self.assertIsNone(files.slug_problem(ctx, "vol3"))
            for bad in ("", "Life", "two words", "-lead", "trail-", "double--hyphen", "under_score", "café"):
                self.assertIsNotNone(files.slug_problem(ctx, bad), repr(bad))
            make_book(ws, "taken", source=False)
            self.assertIn("already exists", files.slug_problem(ctx, "taken"))

    def test_source_rules(self):
        with Workspace() as ws:
            good = fixture("tiny_source.txt")
            self.assertIsNone(files.source_problem(str(good)))
            self.assertIn("Choose", files.source_problem(""))
            self.assertIn("Not a file", files.source_problem(str(ws.base / "missing.txt")))
            other = ws.base / "scan.pdf"
            other.write_bytes(b"%PDF")
            self.assertIn(".txt", files.source_problem(str(other)))

    def test_copy_source_refuses_when_a_file_is_already_there_and_leaves_the_manifest_alone(self):
        with Workspace() as ws:
            ctx = Context.load(ws.config_path)
            make_book(ws, "tiny", source=False)
            before = (ws.book("tiny") / "manifest.yaml").read_bytes()
            target = files.copy_source(ctx, "tiny", fixture("tiny_source.txt"))
            self.assertEqual(target, ws.book("tiny") / "s1b" / "source" / "tiny_source.txt")
            self.assertEqual(target.read_bytes(), fixture("tiny_source.txt").read_bytes())
            with self.assertRaises(FileExistsError):
                files.copy_source(ctx, "tiny", fixture("tiny_candidate_brief.txt"))
            self.assertEqual([p.name for p in target.parent.iterdir()], ["tiny_source.txt"])
            self.assertEqual((ws.book("tiny") / "manifest.yaml").read_bytes(), before)


try:
    import textual  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Textual is not installed (uv sync --extra tui)")

from textual.widgets import DataTable, Input, Static  # noqa: E402
from tui.app import PipelineApp  # noqa: E402
from tui.screens.book import BookScreen  # noqa: E402
from tui.screens.home import HomeScreen  # noqa: E402
from tui.screens.new_book import NewBookScreen  # noqa: E402
from tui.screens.run import RunScreen  # noqa: E402

SIZE = (130, 40)


async def settle(pilot, times=3):
    for _ in range(times):
        await pilot.pause()


class HomeBooksTests(GuardedCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)

    def app(self):
        return PipelineApp(self.ctx, root_group())

    async def test_the_home_table_shows_every_book_newest_first(self):
        seed(self.ws)
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            table = app.screen.query_one(DataTable)
            rows = [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]
            self.assertEqual([r[0] for r in rows], ["failure", "stale", "partial", "fresh"])
            self.assertEqual(rows[0][1:], ["0/13", "1", "-", "not set", "2026-10-05 09:00"])
            self.assertEqual(rows[1][1:4], ["1/13", "-", "1"])
            self.assertEqual(rows[2][1:5], ["2/13", "-", "-", harness.FIXTURE_ISBN])
            self.assertEqual(app.focused, table)

    async def test_with_no_books_it_says_so(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.assertIn("No books yet", app.screen.query_one("#books-empty", Static).render().plain)
            self.assertFalse(app.screen.query_one(DataTable).display)

    async def test_selecting_a_book_opens_it(self):
        seed(self.ws)
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.press("down", "enter")  # second row: "stale"
            await settle(pilot)
            self.assertIsInstance(app.screen, BookScreen)
            self.assertEqual(app.screen.slug, "stale")


class NewBookFlowTests(GuardedCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)

    async def fill(self, pilot, app, slug, source=""):
        app.screen.query_one("#nb-slug", Input).value = slug
        app.screen.query_one("#nb-source", Input).value = source
        await settle(pilot)
        await pilot.click("#nb-create")
        await settle(pilot)

    async def test_invalid_and_duplicate_names_are_rejected_without_running_init(self):
        make_book(self.ws, "taken", source=False)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("n")
            await settle(pilot)
            self.assertIsInstance(app.screen, NewBookScreen)
            source = str(fixture("tiny_source.txt"))
            for slug, expected in (("Bad Name", "lowercase"), ("taken", "already exists"), ("", "Enter a name")):
                await self.fill(pilot, app, slug, source)
                self.assertIsInstance(app.screen, NewBookScreen)
                self.assertIn(expected, app.screen.query_one("#nb-error", Static).render().plain, slug)
            await self.fill(pilot, app, "fine-name", "")
            self.assertIn("Choose the manuscript", app.screen.query_one("#nb-error", Static).render().plain)
            await self.fill(pilot, app, "fine-name", str(self.ws.base / "nope.txt"))
            self.assertIn("Not a file", app.screen.query_one("#nb-error", Static).render().plain)
            self.assertIsNone(app.session)
            self.assertFalse((self.ws.books_dir / "fine-name").exists())

    async def test_the_hint_shows_the_init_command_as_you_type(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("n")
            await settle(pilot)
            app.screen.query_one("#nb-slug", Input).value = "life-as-explorer"
            await settle(pilot)
            self.assertTrue(app.screen.query_one("#nb-hint").text.rstrip().endswith(" init life-as-explorer"))

    async def test_creating_a_book_runs_init_copies_the_manuscript_and_opens_it(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("n")
            await settle(pilot)
            await self.fill(pilot, app, "new-one", str(fixture("tiny_source.txt")))
            # `init` takes about a second; whether the log or the finished book is on screen at this
            # instant depends on timing, so only the run itself is asserted.
            self.assertEqual(app.session.request.title, "init new-one")
            await app.session.wait()
            await settle(pilot, 5)

            root = self.ws.book("new-one")
            self.assertEqual((root / "s1b" / "source" / "tiny_source.txt").read_bytes(),
                             fixture("tiny_source.txt").read_bytes())
            # manifest.yaml is written by `init` alone: it is exactly what init writes
            self.assertEqual((root / "manifest.yaml").read_text(encoding="utf-8"), "slug: new-one\nsource: ~\n")
            self.assertIsInstance(app.screen, BookScreen)
            self.assertEqual(app.screen.slug, "new-one")

            await pilot.press("escape")
            await settle(pilot)
            self.assertIsInstance(app.screen, HomeScreen)
            table = app.screen.query_one(DataTable)
            self.assertEqual(str(table.get_row_at(0)[0]), "new-one")

    async def test_a_failed_init_copies_nothing_and_stays_on_the_log(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("n")
            await settle(pilot)
            (self.ws.books_dir / "race").mkdir()  # appears after the form was validated
            app.screen.query_one("#nb-slug", Input).value = "race"
            app.screen.query_one("#nb-source", Input).value = str(fixture("tiny_source.txt"))
            await settle(pilot)
            from unittest import mock
            with mock.patch("tui.screens.new_book.files.slug_problem", return_value=None):
                await pilot.click("#nb-create")
                await settle(pilot)
            await app.session.wait()
            await settle(pilot, 5)
            self.assertIsInstance(app.screen, RunScreen)  # init failed ("already exists"): no book screen
            self.assertEqual(app.screen.state, "done")
            self.assertEqual(list((self.ws.books_dir / "race").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
