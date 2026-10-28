"""ADR 024 — assisted translation: the work folder, its validation, the `waiting`
ledger status and `s2 run` stopping and resuming around the manual step.

Offline and quick: the "translation" a person would get from the VS Code chat is
simulated by `fake_translate()`, which keeps every paragraph and every markup
token and changes the text. No provider, no network, no quota.
"""
import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
from harness import GuardedCase, Workspace, ledger, make_book, run_cli  # noqa: E402
from lib import assisted_translation as at, manifest, orchestrator, task_loader  # noqa: E402

ASSISTED = {"provider": "assisted", "assisted": {"max_chars": 5000, "model_note": "test-model"}}


def fake_translate(text: str) -> str:
    """Stands in for the chat: same paragraphs, same markup, different text."""
    return text.replace("e", "é")


def paragraph_text(count: int) -> str:
    """`count` paragraphs of ~150 characters, one carrying every markup kind."""
    paras = [f"Paragraph {n} of the test text, long enough to count as real prose for the length check: "
             f"the caravan crossed the desert slowly." for n in range(1, count + 1)]
    paras[1] = paras[1] + " The [i]Wei River[/i] and King Henry [sc]VIII[/sc].\n[FN: A note. [/FN]]"
    return "\n\n".join(paras)


def translate_all(work: Path, index: dict, numbers=None) -> None:
    for n in numbers or range(1, index["count"] + 1):
        source = at.source_path(work, index, n).read_text(encoding="utf-8")
        at.translation_path(work, index, n).write_text(fake_translate(source), encoding="utf-8")


def cli(case, ws, *args, expect=0):
    code, output = run_cli(ws, *args, example=True)
    case.assertEqual(code, expect, f"{' '.join(map(str, args))} exited {code}:\n{output}")
    return output


class ValidationTests(unittest.TestCase):
    SOURCE = paragraph_text(4)

    def problems(self, translated):
        return at.validate_chunk(self.SOURCE, translated)

    def test_a_faithful_translation_passes(self):
        self.assertEqual(self.problems(fake_translate(self.SOURCE)), [])

    def test_a_dropped_paragraph_is_caught(self):
        dropped = "\n\n".join(fake_translate(self.SOURCE).split("\n\n")[:-1])
        self.assertTrue(any("paragraphs" in p for p in self.problems(dropped)), self.problems(dropped))

    def test_unbalanced_markup_is_caught(self):
        broken = fake_translate(self.SOURCE).replace("[/i]", "")
        self.assertTrue(any("markup differs" in p and "[/i]" in p for p in self.problems(broken)))

    def test_a_chat_preamble_and_a_code_fence_are_caught(self):
        self.assertTrue(any("preamble" in p for p in self.problems("Aquí está la traducción:\n\n" + fake_translate(self.SOURCE))))
        self.assertTrue(any("code fence" in p for p in self.problems("```\n" + fake_translate(self.SOURCE) + "\n```")))

    def test_length_outside_the_range_is_caught(self):
        paras = fake_translate(self.SOURCE).split("\n\n")
        shrunk = "\n\n".join(p[:40] for p in paras)
        self.assertTrue(any("length" in p for p in self.problems(shrunk)))

    def test_an_untranslated_copy_and_an_empty_file_are_caught(self):
        self.assertTrue(any("identical" in p for p in self.problems(self.SOURCE)))
        self.assertEqual(self.problems("  \n"), ["the file is empty"])

    def test_dividers_must_stay_where_they_were(self):
        source = "First paragraph.\n\n---\n\nChapter title\n\nBody text."
        self.assertEqual(at.validate_chunk(source, "Primero.\n\n---\n\nTítulo\n\nCuerpo."), [])
        moved = at.validate_chunk(source, "Primero.\n\nTítulo\n\n---\n\nCuerpo.")
        self.assertTrue(any("dividers" in p for p in moved), moved)

    def test_a_short_chunk_is_not_length_checked(self):
        self.assertEqual(at.validate_chunk("---\n\nTHE END", "---\n\nFIN DE LA OBRA, TERMINADA"), [])


class WorkFolderTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = self.root / "cleaned.txt"
        self.source.write_text(paragraph_text(12), encoding="utf-8")
        self.work = self.root / "assisted"

    def tearDown(self):
        self._tmp.cleanup()

    def test_prepare_splits_on_paragraphs_and_everything_is_pending(self):
        index = at.prepare(self.work, self.source, self.root, max_chars=500)
        self.assertGreater(index["count"], 1)
        self.assertEqual(at.pending(self.work, index), list(range(1, index["count"] + 1)))
        rejoined = "\n\n".join(at.source_path(self.work, index, n).read_text(encoding="utf-8")
                               for n in range(1, index["count"] + 1))
        self.assertEqual(rejoined, self.source.read_text(encoding="utf-8"))
        self.assertTrue((self.work / at.GLOSSARY_NAME).is_file())
        self.assertEqual(at.load_index(self.work)["source_sha256"], index["source_sha256"])

    def test_pending_progress_and_ranges(self):
        index = at.prepare(self.work, self.source, self.root, max_chars=500)
        translate_all(self.work, index, [1, 2, 4])
        self.assertEqual(at.pending(self.work, index)[0], 3)
        self.assertEqual(at.progress_text(self.work, index), f"3/{index['count']}")
        self.assertEqual(at._ranges([1, 2, 3, 7, 9, 10]), "1-3, 7, 9-10")

    def test_a_changed_source_is_detected(self):
        index = at.prepare(self.work, self.source, self.root, max_chars=500)
        self.assertFalse(at.source_changed(index, self.source))
        self.source.write_text(self.source.read_text(encoding="utf-8") + "\n\nOne more.", encoding="utf-8")
        self.assertTrue(at.source_changed(index, self.source))

    def test_reject_moves_a_translation_aside_and_makes_it_pending(self):
        index = at.prepare(self.work, self.source, self.root, max_chars=500)
        translate_all(self.work, index)
        at.reject(self.work, index, [2])
        self.assertEqual(at.pending(self.work, index), [2])
        self.assertTrue(at.rejected_path(self.work, index, 2).is_file())


class AssistedFlowTests(GuardedCase):
    """The task end to end through the orchestrator and the CLI."""

    def book(self, ws):
        root = make_book(ws, "tiny")
        return root, root / "s1b" / "translated" / "es" / "assisted"

    def test_s2_run_stops_at_translate_then_resumes_and_merges(self):
        with Workspace(translation=ASSISTED) as ws:
            root, work = self.book(ws)
            first = cli(self, ws, "s2", "run", "tiny", "--only", "s1b")  # exit code 0: waiting is not a failure
            self.assertIn("s1b.translate is waiting", first)
            self.assertIn("Failed: 0", first)
            entries = ledger(root)
            self.assertEqual(entries["s1b.cleanup"]["status"], "done")
            self.assertEqual(entries["s1b.translate"]["status"], "waiting")
            self.assertEqual(entries["s1b.translate"]["progress"], "0/1")
            self.assertNotIn("\n", entries["s1b.translate"]["message"])
            self.assertNotIn("s1b.ortho", entries)  # dependents stay blocked
            self.assertRegex(cli(self, ws, "s2", "status", "tiny"), r"\[w\] translate\s+waiting . 0/1 chunks translated")
            self.assertRegex(cli(self, ws, "book", "list"), r"tiny\s+1/\d+\s+\S+\s+in progress")

            # nothing translated yet: running again just reports the same thing
            self.assertIn("waiting", cli(self, ws, "s2", "run", "tiny", "--only", "s1b"))

            index = at.load_index(work)
            translate_all(work, index)
            cli(self, ws, "s2", "run", "tiny", "--only", "s1b")
            entries = ledger(root)
            for name in ("translate", "ortho", "copyedit"):
                self.assertEqual(entries[f"s1b.{name}"]["status"], "done", name)
            self.assertEqual(entries["s1b.translate"]["provider"], "assisted")
            self.assertEqual(entries["s1b.translate"]["model"], "test-model")
            merged = (root / "s1b" / "translated" / "es" / "tiny_source.txt").read_text(encoding="utf-8")
            self.assertEqual(merged.strip(), fake_translate((root / "s1b" / "cleaned" / "tiny_source.txt")
                                                              .read_text(encoding="utf-8")).strip())

    def test_status_option_is_read_only(self):
        with Workspace(translation=ASSISTED) as ws:
            root, work = self.book(ws)
            cleaned = root / "s1b" / "cleaned" / "tiny_source.txt"
            cli(self, ws, "s1b", "cleanup", root / "s1b" / "source" / "tiny_source.txt")
            output = cli(self, ws, "s1b", "translate", cleaned, "--status")
            self.assertIn("Not prepared yet", output)
            self.assertFalse(work.exists())
            self.assertNotIn("s1b.translate", ledger(root))

            cli(self, ws, "s1b", "translate", cleaned)  # prepares
            before = manifest.load(root)["tasks"]["s1b.translate"]
            output = cli(self, ws, "s1b", "translate", cleaned, "--status")
            self.assertIn("0/1 chunks translated; pending: 1", output)
            self.assertEqual(manifest.load(root)["tasks"]["s1b.translate"], before)

    def test_a_failing_chunk_is_moved_aside_and_redone(self):
        with Workspace(translation=ASSISTED) as ws:
            root, work = self.book(ws)
            cli(self, ws, "s2", "run", "tiny", "--only", "s1b")
            index = at.load_index(work)
            translate_all(work, index)
            good = at.translation_path(work, index, 1).read_text(encoding="utf-8")
            at.translation_path(work, index, 1).write_text("Aquí está la traducción:\n\n" + good, encoding="utf-8")

            output = cli(self, ws, "s2", "run", "tiny", "--only", "s1b")
            self.assertIn("starts with a chat preamble", output)
            self.assertEqual(ledger(root)["s1b.translate"]["status"], "waiting")
            self.assertFalse(at.translation_path(work, index, 1).exists())
            self.assertTrue(at.rejected_path(work, index, 1).is_file())
            self.assertFalse((root / "s1b" / "translated" / "es" / "tiny_source.txt").exists())

            at.translation_path(work, index, 1).write_text(good, encoding="utf-8")
            cli(self, ws, "s2", "run", "tiny", "--only", "s1b")
            self.assertEqual(ledger(root)["s1b.translate"]["status"], "done")

    def test_a_changed_source_fails_until_restarted_and_keeps_the_old_folder(self):
        with Workspace(translation=ASSISTED) as ws:
            root, work = self.book(ws)
            cli(self, ws, "s2", "run", "tiny", "--only", "s1b")
            cleaned = root / "s1b" / "cleaned" / "tiny_source.txt"
            cleaned.write_text(cleaned.read_text(encoding="utf-8") + "\n\nA paragraph added later.", encoding="utf-8")

            output = cli(self, ws, "s1b", "translate", cleaned, expect=1)
            self.assertIn("changed since the chunks", output)
            self.assertEqual(ledger(root)["s1b.translate"]["status"], "failed")

            cli(self, ws, "s1b", "translate", cleaned, "--restart")
            self.assertEqual(ledger(root)["s1b.translate"]["status"], "waiting")
            self.assertEqual(len(list(work.parent.glob("assisted.old-*"))), 1)
            self.assertFalse(at.source_changed(at.load_index(work), cleaned))

    def test_waiting_is_not_a_failure_in_the_orchestrator_summary(self):
        with Workspace(translation=ASSISTED) as ws, harness.example_manifests():
            root, _ = self.book(ws)
            with contextlib.redirect_stdout(io.StringIO()):
                summary = orchestrator.run_book(root, ws.config, only="s1b")
            self.assertEqual(summary["done"], ["s1b.cleanup"])
            self.assertEqual(summary["failed"], [])
            self.assertEqual(summary["waiting"], ["s1b.translate"])
            self.assertFalse(summary["complete"])
            row = next(r for r in orchestrator.book_status(root) if r["name"] == "translate")
            self.assertEqual((row["status"], row["ready_to_continue"]), ("waiting", True))

    def test_other_providers_reject_the_assisted_only_options(self):
        with Workspace() as ws:  # translation.provider: fake
            root = make_book(ws, "tiny")
            cli(self, ws, "s1b", "cleanup", root / "s1b" / "source" / "tiny_source.txt")
            output = cli(self, ws, "s1b", "translate", root / "s1b" / "cleaned" / "tiny_source.txt", "--status", expect=1)
            self.assertIn("only apply to translation.provider: assisted", output)


if __name__ == "__main__":
    unittest.main()
