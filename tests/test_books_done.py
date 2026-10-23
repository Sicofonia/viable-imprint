"""Marking a book as done: `lib/books.py` and the `book done`, `book reopen` and `book list`
commands, plus the `s2 status` line. A done book is only listed differently; nothing else
about it changes.
"""
import sys
import unittest
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
from harness import FIXTURE_ISBN, GuardedCase, Workspace, ledger, make_book, run_cli  # noqa: E402
from lib import books, manifest, orchestrator  # noqa: E402


def cli(case, ws, *args, expect=0):
    code, out = run_cli(ws, *args, example=True)
    case.assertEqual(code, expect, f"{' '.join(args)} exited {code}:\n{out}")
    return out.replace("\r\n", "\n")


class LibraryTests(GuardedCase):
    def test_marking_writes_one_key_and_keeps_everything_else(self):
        with Workspace() as ws:
            root = make_book(ws, "tiny", source=False)
            manifest.update(root, isbn=FIXTURE_ISBN)
            manifest.record_task(root, "s1b.cleanup", status="done", output="x", completed_at="2026-10-01T00:00:00+00:00")
            manifest.record_sale(root, [{"platform": "kdp", "period_start": "a", "period_end": "b", "revenue": 1.0}])
            before = manifest.load(root)

            self.assertEqual(books.mark_done(root, on=date(2026, 10, 23)), (True, "2026-10-23"))
            after = manifest.load(root)
            self.assertEqual(after.pop("done_on"), "2026-10-23")
            self.assertEqual(after, before)           # the ledger, the sales and the ISBN are untouched
            self.assertEqual(books.done_on(root), "2026-10-23")

    def test_marking_twice_keeps_the_first_date_and_writes_nothing(self):
        with Workspace() as ws:
            root = make_book(ws, "tiny", source=False)
            books.mark_done(root, on=date(2026, 1, 5))
            written = (root / "manifest.yaml").read_bytes()
            self.assertEqual(books.mark_done(root, on=date(2026, 10, 23)), (False, "2026-01-05"))
            self.assertEqual((root / "manifest.yaml").read_bytes(), written)

    def test_reopening_removes_only_the_mark(self):
        with Workspace() as ws:
            root = make_book(ws, "tiny", source=False)
            manifest.update(root, isbn=FIXTURE_ISBN)
            self.assertFalse(books.reopen(root))      # not marked: nothing to do
            books.mark_done(root)
            self.assertTrue(books.reopen(root))
            self.assertIsNone(books.done_on(root))
            self.assertEqual(manifest.load(root).get("isbn"), FIXTURE_ISBN)
            self.assertNotIn("done_on", manifest.load(root))

    def test_a_task_that_is_done_is_not_a_book_that_is_done(self):
        with Workspace() as ws, harness.example_manifests():
            root = make_book(ws, "tiny", source=False)
            for node in orchestrator._load_graph():
                manifest.record_task(root, f"{node['system']}.{node['task']['name']}", status="done", output="x",
                                     completed_at="2026-10-01T00:00:00+00:00")
            self.assertIsNone(books.done_on(root))

    def test_ordering_puts_books_in_progress_first_and_each_group_newest_first(self):
        items = [("a", None, "2026-10-01"), ("b", "2026-10-20", "2026-10-19"), ("c", None, "2026-10-05"),
                 ("d", "2026-09-01", "2026-09-01"), ("e", None, "2026-10-03")]
        ordered = books.order(items, lambda i: i[1], lambda i: i[2])
        self.assertEqual([i[0] for i in ordered], ["c", "e", "a", "b", "d"])
        self.assertEqual(books.order([], lambda i: i, lambda i: i), [])


class CommandTests(GuardedCase):
    def test_done_then_status_then_reopen(self):
        with Workspace() as ws:
            root = make_book(ws, "tiny")
            out = cli(self, ws, "book", "done", "tiny")
            self.assertIn(f"Marked tiny as done ({date.today().isoformat()}).", out)
            self.assertIn("Note: only 0 of its 13 tasks are done.", out)
            self.assertEqual(books.done_on(root), date.today().isoformat())

            status = cli(self, ws, "s2", "status", "tiny")
            self.assertIn(f"Marked as done on {date.today().isoformat()}.", status)

            self.assertIn("was already marked as done", cli(self, ws, "book", "done", "tiny"))
            self.assertIn("Reopened tiny.", cli(self, ws, "book", "reopen", "tiny"))
            self.assertIsNone(books.done_on(root))
            self.assertNotIn("Marked as done", cli(self, ws, "s2", "status", "tiny"))
            self.assertIn("tiny was not marked as done.", cli(self, ws, "book", "reopen", "tiny"))

    def test_no_note_when_every_task_is_done(self):
        with Workspace() as ws, harness.example_manifests():
            root = make_book(ws, "tiny", source=False)
            for node in orchestrator._load_graph():
                manifest.record_task(root, f"{node['system']}.{node['task']['name']}", status="done", output="x",
                                     completed_at="2026-10-01T00:00:00+00:00")
            out = cli(self, ws, "book", "done", "tiny")
            self.assertIn("Marked tiny as done", out)
            self.assertNotIn("Note:", out)

    def test_an_unknown_book_is_an_error_for_done_and_reopen(self):
        with Workspace() as ws:
            for verb in ("done", "reopen"):
                self.assertIn("No manifest.yaml found", cli(self, ws, "book", verb, "nope", expect=1))

    def test_list_shows_books_in_progress_first_and_done_books_last_with_a_mark(self):
        with Workspace() as ws:
            for slug in ("alpha", "beta", "gamma"):
                make_book(ws, slug)
            manifest.record_task(ws.book("alpha"), "s1b.cleanup", status="done", output="x",
                                 completed_at="2026-10-02T00:00:00+00:00")
            manifest.update(ws.book("gamma"), isbn=FIXTURE_ISBN)
            cli(self, ws, "book", "done", "alpha")

            lines = cli(self, ws, "book", "list").splitlines()
            self.assertEqual(lines[0].split(), ["Book", "Tasks", "ISBN", "Status"])
            rows = {l.split()[0]: l for l in lines[1:]}
            self.assertEqual([l.split()[0] for l in lines[1:]][-1], "alpha")             # done last
            self.assertEqual(set(rows), {"alpha", "beta", "gamma"})
            self.assertIn(f"[x] done {date.today().isoformat()}", rows["alpha"])
            self.assertIn("1/13", rows["alpha"])
            self.assertIn("not started", rows["beta"])
            self.assertIn(FIXTURE_ISBN, rows["gamma"])

    def test_list_hints_when_every_task_is_done_but_the_book_is_not_marked(self):
        with Workspace() as ws, harness.example_manifests():
            root = make_book(ws, "tiny", source=False)
            for node in orchestrator._load_graph():
                manifest.record_task(root, f"{node['system']}.{node['task']['name']}", status="done", output="x",
                                     completed_at="2026-10-01T00:00:00+00:00")
            self.assertIn("all tasks done (not marked: `book done`)", cli(self, ws, "book", "list"))

    def test_list_with_in_progress_work_and_with_no_books(self):
        with Workspace() as ws:
            self.assertIn("No books found", cli(self, ws, "book", "list"))
            root = make_book(ws, "tiny")
            manifest.record_task(root, "s1b.cleanup", status="failed", error="boom", attempted_at="2026-10-02T00:00:00+00:00")
            self.assertIn("in progress", cli(self, ws, "book", "list"))

    def test_a_done_book_is_otherwise_a_book_like_any_other(self):
        with Workspace() as ws:
            root = make_book(ws, "tiny")
            cli(self, ws, "book", "done", "tiny")
            cli(self, ws, "s2", "run", "tiny", "--step")                        # it can still be worked on
            self.assertEqual(ledger(root)["s1b.cleanup"]["status"], "done")
            self.assertEqual(books.done_on(root), date.today().isoformat())      # and stays marked
            dashboard = cli(self, ws, "s3", "dashboard")                         # and stays in the figures
            self.assertIn("tiny", dashboard)
            self.assertIn("Portfolio: 1 book", dashboard)


class ManifestFormatTests(GuardedCase):
    def test_the_key_is_a_plain_date_string_in_the_manifest(self):
        with Workspace() as ws:
            root = make_book(ws, "tiny", source=False)
            books.mark_done(root, on=date(2026, 10, 23))
            data = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
            self.assertEqual(data["done_on"], "2026-10-23")
            self.assertIsInstance(data["done_on"], str)


if __name__ == "__main__":
    unittest.main()
