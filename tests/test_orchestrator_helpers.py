"""ADR 023, T2.3 — `orchestrator.resolve_input` and `downstream_of`, the two read-only
helpers the terminal UI uses. Pinned against the behaviour they must agree with:
`ready_tasks()` (which input a task gets) and `cascade_invalidate()` (what goes stale).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
from harness import GuardedCase, Workspace, ledger, make_book  # noqa: E402
from lib import manifest, orchestrator  # noqa: E402


def mark_done(root, key, output):
    path = root / output
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    manifest.record_task(root, key, status="done", output=output, completed_at="2026-10-01T00:00:00+00:00",
                         output_mtime=path.stat().st_mtime)


class ResolveInputTests(GuardedCase):
    def test_agrees_with_ready_tasks_for_every_ready_task(self):
        with Workspace() as ws, harness.example_manifests():
            root = make_book(ws, "tiny")
            mark_done(root, "s1b.cleanup", "s1b/cleaned/tiny_source.txt")
            mark_done(root, "s1b.translate", "s1b/translated/es/tiny_source.txt")
            mark_done(root, "s1b.ortho", "s1b/ortho/es/tiny_source.txt")
            mark_done(root, "s1b.copyedit", "s1b/copyedit/es/tiny_source.txt")
            mark_done(root, "s1d.brief", "s1d/brief/es/tiny_source.txt")
            ready = orchestrator.ready_tasks(root)
            self.assertGreater(len(ready), 5)  # format and the seven brief fan-out tasks
            for node, input_path in ready:
                key = f"{node['system']}.{node['task']['name']}"
                self.assertEqual(orchestrator.resolve_input(root, key), (input_path, None), key)

    def test_a_done_task_resolves_to_the_same_input_it_ran_with(self):
        with Workspace() as ws, harness.example_manifests():
            root = make_book(ws, "tiny")
            mark_done(root, "s1b.cleanup", "s1b/cleaned/tiny_source.txt")
            mark_done(root, "s1b.translate", "s1b/translated/es/tiny_source.txt")
            self.assertEqual(orchestrator.resolve_input(root, "s1b.cleanup"),
                             (root / "s1b" / "source" / "tiny_source.txt", None))
            self.assertEqual(orchestrator.resolve_input(root, "s1b.translate"),
                             (root / "s1b" / "cleaned" / "tiny_source.txt", None))

    def test_reasons_when_there_is_no_input(self):
        with Workspace() as ws, harness.example_manifests():
            root = make_book(ws, "tiny")
            self.assertEqual(orchestrator.resolve_input(root, "s1b.translate"), (None, "waiting on s1b.cleanup"))
            empty = make_book(ws, "empty", source=False)
            self.assertEqual(orchestrator.resolve_input(empty, "s1b.cleanup"), (None, "no file in s1b/source/"))
            (root / "s1b" / "source" / "second.txt").write_text("y", encoding="utf-8")
            path, reason = orchestrator.resolve_input(root, "s1b.cleanup")
            self.assertIsNone(path)
            self.assertIn("expected exactly one", reason)

    def test_a_key_outside_the_book_graph_is_an_error(self):
        with Workspace() as ws, harness.example_manifests():
            root = make_book(ws, "tiny")
            for key in ("s5.homeostat", "s1d.newsletter", "nonsense"):
                with self.assertRaises(KeyError):
                    orchestrator.resolve_input(root, key)

    def test_it_changes_nothing_on_disk(self):
        with Workspace() as ws, harness.example_manifests():
            root = make_book(ws, "tiny")
            before = (root / "manifest.yaml").read_bytes()
            orchestrator.resolve_input(root, "s1b.cleanup")
            orchestrator.resolve_input(root, "s1b.translate")
            self.assertEqual((root / "manifest.yaml").read_bytes(), before)


class DownstreamOfTests(GuardedCase):
    def test_equals_what_the_cascade_marks_stale(self):
        with Workspace() as ws, harness.example_manifests():
            for start in ("s1b.translate", "s1b.cleanup", "s1d.brief", "s1d.synopsis"):
                root = make_book(ws, "b-" + start.replace(".", "-"))
                for node in orchestrator._load_graph():
                    key = f"{node['system']}.{node['task']['name']}"
                    mark_done(root, key, f"{node['system']}/{node['task']['name']}/x.txt")
                orchestrator.cascade_invalidate(root, start)
                stale = {k for k, v in ledger(root).items() if v["status"] == "stale"}
                self.assertEqual(stale, set(orchestrator.downstream_of(start)), start)

    def test_order_is_nearest_first_and_a_leaf_has_nothing_downstream(self):
        with harness.example_manifests():
            self.assertEqual(orchestrator.downstream_of("s1b.cleanup")[:3], ["s1b.translate", "s1b.ortho", "s1b.copyedit"])
            self.assertEqual(orchestrator.downstream_of("s1d.metadata"), [])
            self.assertEqual(orchestrator.downstream_of("not.a.task"), [])


if __name__ == "__main__":
    unittest.main()
