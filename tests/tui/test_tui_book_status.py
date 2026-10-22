"""ADR 023, T2.2 — the book screen's status view: every task state, the
`edited since run` marker, blocking reasons, parity with `s2 status`, refreshing.
"""
import logging
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402
from harness import EXAMPLE_LAUNCHER, GuardedCase, Workspace, make_book, root_group, run_cli  # noqa: E402
from lib import manifest  # noqa: E402
from tui.services import state  # noqa: E402
from tui.services.context import Context  # noqa: E402

logging.getLogger("asyncio").setLevel(logging.ERROR)


def seed_every_state(ws, slug="states"):
    """A ledger with every state `orchestrator.book_status` can report."""
    root = make_book(ws, slug)
    cleaned = root / "s1b" / "cleaned" / "tiny_source.txt"
    cleaned.parent.mkdir(parents=True)
    cleaned.write_text("cleaned", encoding="utf-8")
    brief = root / "s1d" / "brief" / "es" / "tiny_source.txt"
    brief.parent.mkdir(parents=True)
    brief.write_text("brief", encoding="utf-8")
    record = lambda key, **fields: manifest.record_task(root, key, **fields)
    record("s1b.cleanup", status="done", output="s1b/cleaned/tiny_source.txt", completed_at="2026-10-01T10:00:00+00:00",
           output_mtime=cleaned.stat().st_mtime)                                   # done, untouched
    record("s1b.translate", status="stale", invalidated_by="s1b.cleanup", output="s1b/translated/es/tiny_source.txt")
    record("s1b.ortho", status="failed", error="boom", attempted_at="2026-10-01T11:00:00+00:00")
    record("s1b.copyedit", status="stale", invalidated_by="s1b.cleanup", output="s1b/copyedit/es/tiny_source.txt")
    record("s1d.brief", status="done", output="s1d/brief/es/tiny_source.txt", completed_at="2026-10-01T12:00:00+00:00",
           output_mtime=brief.stat().st_mtime - 1000)                              # done, edited by hand since
    record("s1d.story-map", status="failed", error="timeout", attempted_at="2026-10-01T13:00:00+00:00")
    return root


EXPECTED = {
    "cleanup": ("done", "s1b/cleaned/tiny_source.txt"),
    "translate": ("stale", "invalidated by s1b.cleanup - ready to rerun"),
    "ortho": ("failed", "boom - blocked"),
    "copyedit": ("stale", "invalidated by s1b.cleanup - blocked"),
    "format": ("blocked", "waiting on s1b.copyedit"),
    "brief": ("done", "s1d/brief/es/tiny_source.txt (edited since run)"),
    "synopsis": ("ready", "ready"),
    "story-map": ("failed", "timeout - ready to retry"),
    "one-pager": ("ready", "ready"),
    "press-dossier": ("ready", "ready"),
    "trailer-storyboard": ("ready", "ready"),
    "goodreads-profile": ("ready", "ready"),
    "metadata": ("ready", "ready"),
}


class TaskStateTests(GuardedCase):
    def test_every_state_is_described_with_the_words_of_s2_status(self):
        with Workspace() as ws:
            rows = {r.name: r for r in state.book_tasks(seed_every_state(ws))}
            self.assertEqual({n: (r.status, r.detail) for n, r in rows.items()}, EXPECTED)
            self.assertTrue(rows["brief"].edited)
            self.assertFalse(rows["cleanup"].edited)
            self.assertEqual(rows["translate"].invalidated_by, "s1b.cleanup")

    def test_which_rows_can_be_run(self):
        with Workspace() as ws:
            rows = {r.name: r.runnable for r in state.book_tasks(seed_every_state(ws))}
            self.assertEqual({n for n, ok in rows.items() if not ok}, {"ortho", "copyedit", "format"})  # the blocked ones
            self.assertTrue(rows["cleanup"])      # done: re-running is allowed (with a warning)
            self.assertTrue(rows["translate"])    # stale and ready to rerun
            self.assertTrue(rows["story-map"])    # failed and ready to retry

    def test_zero_and_two_source_files_block_the_first_task_with_the_reason(self):
        with Workspace() as ws:
            make_book(ws, "empty", source=False)
            two = make_book(ws, "two")
            (two / "s1b" / "source" / "second.txt").write_text("another", encoding="utf-8")
            first = lambda slug: state.book_tasks(ws.book(slug))[0]
            self.assertEqual((first("empty").status, first("empty").detail), ("blocked", "no file in s1b/source/"))
            self.assertEqual((first("two").status, first("two").detail),
                             ("blocked", "2 files in s1b/source/ — expected exactly one"))
            self.assertEqual(first("two").runnable, False)

    def test_the_rows_match_s2_status_for_the_same_book(self):
        with Workspace() as ws:
            seed_every_state(ws)
            code, out = run_cli(ws, "s2", "status", "states", example=True)
            self.assertEqual(code, 0, out)
            cli = {}
            for line in out.splitlines():
                m = re.match(r"^\s+\[(.)\] (\S+)\s*(.*)$", line)
                if m:
                    mark, name, rest = m.groups()
                    cli[name] = {"x": "done", "~": "stale", "!": "failed"}.get(mark, "ready" if rest == "ready" else "blocked")
            ours = {r.name: r.status for r in state.book_tasks(ws.book("states"))}
            self.assertEqual(cli, ours)
            self.assertEqual(len(ours), 13)


try:
    import textual  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Textual is not installed (uv sync --extra tui)")

from textual.widgets import DataTable, Static  # noqa: E402
from tui.app import PipelineApp  # noqa: E402
from tui.screens.book import BookScreen  # noqa: E402
from tui.services.session import RunRequest  # noqa: E402

SIZE = (150, 45)


async def settle(pilot, times=3):
    for _ in range(times):
        await pilot.pause()


class BookScreenTests(GuardedCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)

    def cells(self, table, key):
        return [str(c) for c in table.get_row(key)]

    async def test_the_table_shows_every_state_grouped_by_system(self):
        seed_every_state(self.ws)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            app.open_book("states")
            await settle(pilot)
            screen = app.screen
            self.assertIsInstance(screen, BookScreen)
            table = screen.query_one(DataTable)
            self.assertEqual(table.row_count, 13 + 2)  # tasks plus a heading per system
            self.assertEqual(self.cells(table, "hdr:s1b")[0], "System 1B — Editorial Production")
            self.assertEqual(self.cells(table, "hdr:s1d")[0], "System 1D — Publication and Marketing")
            for name, (status, detail) in EXPECTED.items():
                system = "s1d" if name in {"brief", "synopsis", "story-map", "one-pager", "press-dossier",
                                           "trailer-storyboard", "goodreads-profile", "metadata"} else "s1b"
                cells = self.cells(table, f"{system}.{name}")
                self.assertEqual((cells[1], cells[2]), (status, detail), name)
                self.assertTrue(cells[0].endswith(name))
            self.assertEqual(self.cells(table, "s1b.cleanup")[0], "[x] cleanup")
            self.assertEqual(self.cells(table, "s1b.translate")[0], "[~] translate")
            self.assertEqual(self.cells(table, "s1b.ortho")[0], "[!] ortho")
            title = screen.query_one("#book-title", Static).render().plain
            self.assertIn("states", title)
            self.assertIn("ISBN: not set", title)
            self.assertIn("2/13 done", title)
            self.assertIn("2 failed", title)
            self.assertIn("2 stale", title)

    async def test_the_detail_panel_follows_the_cursor(self):
        seed_every_state(self.ws)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            app.open_book("states")
            await settle(pilot)
            screen = app.screen
            self.assertTrue(screen.query_one("#task-detail", Static).render().plain.startswith("Select a task"))  # heading row
            screen.query_one(DataTable).move_cursor(row=screen.query_one(DataTable).get_row_index("s1b.ortho"))
            await settle(pilot)
            detail = screen.query_one("#task-detail", Static).render().plain
            self.assertIn("s1b ortho", detail)
            self.assertIn("State: failed", detail)
            self.assertIn("boom - blocked", detail)
            self.assertEqual(screen.selected().name, "ortho")

    async def test_a_book_with_no_source_file_shows_the_reason(self):
        make_book(self.ws, "empty", source=False)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            app.open_book("empty")
            await settle(pilot)
            cells = self.cells(app.screen.query_one(DataTable), "s1b.cleanup")
            self.assertEqual((cells[1], cells[2]), ("blocked", "no file in s1b/source/"))

    async def test_f5_re_reads_the_ledger(self):
        root = make_book(self.ws, "tiny")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            app.open_book("tiny")
            await settle(pilot)
            table = app.screen.query_one(DataTable)
            self.assertEqual(self.cells(table, "s1b.cleanup")[1], "ready")
            manifest.record_task(root, "s1b.cleanup", status="failed", error="changed outside",
                                 attempted_at="2026-10-02T00:00:00+00:00")
            await pilot.press("f5")
            await settle(pilot)
            self.assertEqual(self.cells(table, "s1b.cleanup")[1:], ["failed", "changed outside - ready to retry"])

    async def test_the_screen_refreshes_itself_when_a_run_ends(self):
        make_book(self.ws, "tiny")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            app.open_book("tiny")
            await settle(pilot)
            screen = app.screen
            from tui.services import commands
            from tui.services.command_model import build_model
            spec = next(s for s in build_model(root_group()) if s.name == "s2 run")
            argv = commands.argv_for(self.ctx, spec, {"book_slug": "tiny", "step": True})
            app.begin_run(RunRequest("s2 run tiny --step", argv))
            await settle(pilot)
            await app.session.wait()
            await settle(pilot, 5)
            self.assertEqual(self.cells(screen.query_one(DataTable), "s1b.cleanup")[1], "done")
            self.assertEqual(self.cells(screen.query_one(DataTable), "s1b.translate")[1], "ready")


if __name__ == "__main__":
    unittest.main()
