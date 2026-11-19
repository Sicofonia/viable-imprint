"""ADR 025 — the deterministic OCR pre-pass (`lib/ocr_prepass.py`, `pipeline.py s1b prepass`).

Offline and instant. Every fixture here is synthetic and imitates the *layout* of a
scanned book (page number, blank line, running header, one sentence per line), not
any real text.
"""
import re
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import GuardedCase, Workspace, ledger, make_book, run_cli  # noqa: E402
from lib import ocr_prepass as op, orchestrator  # noqa: E402

BODY = [
    "The caravan left the village before dawn and",
    "followed the river for some hours.",
    "Nobody spoke.",
    "By noon the wind had risen, and we halted",
    "beneath an overhang to brew tea.",
]


def book(pages, *, numbers=True):
    """A synthetic scan: `pages` is a list of (header or None, [lines]). Each page ends
    with its number, then a blank line, then the next page's running header."""
    out = []
    for n, (header, lines) in enumerate(pages, 1):
        if header:
            out.append(header)
        out.extend(lines)
        if numbers:
            out.append(str(n))
        out.append("")
    return "\n".join(out)


def words(text):
    return re.findall(r"[A-Za-z]+", text)


class PageApparatusTests(unittest.TestCase):
    def test_page_numbers_and_running_headers_are_removed_and_text_is_joined_across_pages(self):
        text = book([
            (None, ["A sentence that starts on the first page and"]),
            ("THE BOOK", ["carries on over the page break, ending here."]),
            ("CHAPTER ONE TITLE", ["Second thought follows directly."]),
            ("THE BOOK", ["Third thought."]),
        ])
        out, report = op.prepass_text(text)
        self.assertIn("starts on the first page and carries on over the page break, ending here.", out)
        self.assertEqual(report.page_numbers_clean, 4)
        self.assertNotRegex(out, r"(?m)^\d+$")

    def test_a_header_repeated_on_several_pages_is_confirmed_and_removed(self):
        pages = [(None, BODY)] + [("THE BOOK" if i % 2 else "SOME TITLE", BODY) for i in range(6)]
        out, report = op.prepass_text(book(pages))
        self.assertNotIn("THE BOOK", out)
        self.assertNotIn("SOME TITLE", out)
        self.assertEqual(sum(report.headers_removed.values()), 6)

    def test_an_unrepeated_caps_line_is_kept_and_reported_never_silently_deleted(self):
        text = book([(None, BODY), ("A LONE CAPS LINE", BODY)])
        out, report = op.prepass_text(text)
        self.assertIn("A LONE CAPS LINE", out)
        self.assertIn("A LONE CAPS LINE", report.unconfirmed_headers)

    def test_garbled_page_numbers_are_removed_but_a_hyphen_fragment_is_not(self):
        text = "\n".join(BODY + ["Be)", ""] + BODY[:2] + ["a word that gets broken at the page end and", "In-", "", "tonation follows."])
        out, report = op.prepass_text(text)
        self.assertNotIn("Be)", out)
        self.assertEqual(report.page_number_anomalies, ["Be)"])
        self.assertIn("In", out)
        self.assertNotIn(("page number", "In-"), report.removed)
        self.assertNotIn(("garbled page number", "In-"), report.removed)

    def test_page_number_sequence_gaps_are_reported_not_fixed(self):
        text = "\n".join(BODY + ["1", ""] + BODY + ["2", ""] + BODY + ["7", ""])
        _, report = op.prepass_text(text)
        self.assertEqual(report.page_number_gaps, [(2, 7)])


class ChapterTests(unittest.TestCase):
    def chapter_book(self, first_heading):
        """Three chapters; every page after the first of a chapter carries running
        headers (book title on odd pages, chapter title on even ones)."""
        pages, titles = [], ["THE FIRST ROAD", "THE SECOND ROAD", "THE THIRD ROAD"]
        for k, title in enumerate(titles):
            heading = first_heading(k, title)
            pages.append((None, heading + ["Opening words of the chapter, and then more text to fill the line out."] + BODY))
            for p in range(4):
                pages.append((title if p % 2 == 0 else "THE BOOK", BODY))
        return book(pages)

    def test_clean_headings_are_numbered_in_order_and_standardised(self):
        text = self.chapter_book(lambda k, t: ["CHAPTER", ["I.", "II.", "III."][k], t + "."])
        out, report = op.prepass_text(text)
        self.assertEqual([(n, title) for _, n, title, _ in report.chapters],
                         [(1, "THE FIRST ROAD"), (2, "THE SECOND ROAD"), (3, "THE THIRD ROAD")])
        self.assertIn("CHAPTER I\nTHE FIRST ROAD\n\n", out)
        self.assertIn("CHAPTER III\nTHE THIRD ROAD\n\n", out)

    def test_an_unreadable_numeral_is_renumbered_by_order_and_ocr_variants_of_the_word_chapter_work(self):
        text = self.chapter_book(lambda k, t: ["GHAPTER", ["IL.", "II.", "III."][k], t + "."])
        out, report = op.prepass_text(text)
        self.assertEqual([n for _, n, _, _ in report.chapters], [1, 2, 3])
        self.assertNotIn("GHAPTER", out)

    def test_a_mangled_heading_is_recovered_from_its_running_header(self):
        def heading(k, title):
            return ["CHAPTER", "I.", title + "."] if k != 1 else ["CHA PLE", "Rexel eee", "THE SEC OND RODD."]
        out, report = op.prepass_text(self.chapter_book(heading))
        self.assertEqual([n for _, n, _, _ in report.chapters], [1, 2, 3])
        self.assertIn("CHAPTER II\nTHE SECOND ROAD\n\n", out)
        self.assertNotIn("Rexel", out)
        self.assertTrue(any("THE SECOND ROAD" in c for c in report.chapter_checks), report.chapter_checks)

    def test_the_book_title_header_is_never_mistaken_for_a_chapter(self):
        out, report = op.prepass_text(self.chapter_book(lambda k, t: ["CHAPTER", ["I.", "II.", "III."][k], t + "."]))
        self.assertEqual(len(report.chapters), 3)
        self.assertNotIn("CHAPTER IV", out)

    def test_a_book_with_only_one_kind_of_header_invents_no_chapters(self):
        pages = [(None, BODY)] + [("THE BOOK", BODY) for _ in range(5)]
        _, report = op.prepass_text(book(pages))
        self.assertEqual(report.chapters, [])

    def test_chapter_lead_in_capitals_are_sentence_cased(self):
        text = book([(None, ["CHAPTER", "I.", "A TITLE."] + ["ONE evening the caravan left the village and", "walked on."] + BODY)])
        out, _ = op.prepass_text(text)
        self.assertIn("\n\nOne evening the caravan left", out)


class HyphenTests(unittest.TestCase):
    def run_lines(self, *lines):
        return op.prepass_text("\n".join(lines))

    def test_a_split_word_attested_solid_elsewhere_is_joined(self):
        out, report = self.run_lines("The exploration was long, and the explor-", "ation of the pass took days.", "Exploration continues.")
        self.assertIn("the exploration of the pass", out)
        self.assertEqual(report.hyphens_joined, 1)

    def test_a_compound_prefix_or_a_capitalised_second_half_keeps_the_hyphen(self):
        out, report = self.run_lines("He was a self-", "respecting man from Kender-", "Davan, and he was thirty-", "four years old.")
        self.assertIn("self-respecting", out)
        self.assertIn("Kender-Davan", out)
        self.assertIn("thirty-four", out)
        self.assertEqual(report.hyphens_unresolved, [])

    def test_an_unclassifiable_hyphen_is_marked_for_the_llm(self):
        out, report = self.run_lines("It was a considerable discom-", "fort to all of us.")
        self.assertIn("discom{~}fort", out)
        self.assertEqual(report.hyphens_unresolved, ["discom{~}fort"])

    def test_an_inline_hyphen_followed_by_a_space_is_treated_the_same_way(self):
        out, _ = self.run_lines("They went with- out much hope. Without it nothing worked, and with- out luck neither.")
        self.assertNotIn("with- out", out)

    def test_a_hyphen_before_a_footnote_is_left_alone_and_reported(self):
        text = "\n".join(BODY[:2] + ["the inhabi-", "1 A footnote about it.", "", "tants were few."])
        _, report = op.prepass_text(text)
        self.assertTrue(report.hyphens_unjoined or report.footnotes)


class TextRepairTests(unittest.TestCase):
    def repaired(self, *lines):
        return op.prepass_text("\n".join(lines))[0]

    def test_split_ellipses_are_repaired(self):
        out = self.repaired("He said, “Please", ".", ".", ". really, you know", ".", ".", ". it is cold.”")
        self.assertIn("Please ... really, you know ... it is cold", out)

    def test_spaces_before_question_marks_and_garbled_quotes_are_repaired(self):
        out = self.repaired("‘“‘ Why do you ask ? ’’ he said.", "‘‘ Because I must ; ”’ I replied.")
        self.assertIn("“Why do you ask?” he said.", out)
        self.assertIn("“Because I must; ” I replied.".replace("; ”", ";”"), out.replace("; ”", ";”"))

    def test_certain_ocr_misreadings_are_fixed_and_nothing_else_is_guessed(self):
        out = self.repaired("Tf you go, T am sure that T will follow.", "The phi flung back the roll.")
        self.assertIn("If you go, I am sure that I will follow.", out)
        self.assertIn("phi flung", out)     # junk tokens are the LLM's job

    def test_a_footnote_at_the_page_foot_is_wrapped_in_place(self):
        text = "\n".join(BODY[:3] + ["1 This is the footnote text.", "7", ""] + BODY[3:] + ["8", ""])
        out, report = op.prepass_text(text)
        self.assertEqual(report.footnotes, 1)
        self.assertIn("[FN: This is the footnote text. [/FN]]", out)

    def test_lone_junk_lines_are_deleted_but_real_one_word_lines_are_not(self):
        out = self.repaired("The road was long", "|", "and we", "I", "walked on.")
        self.assertNotIn("|", out)
        self.assertIn("and we I walked on.", out)


class ParagraphTests(unittest.TestCase):
    def test_a_dialogue_turn_opens_a_provisional_block(self):
        out = op.prepass_text("\n".join([
            "“Where are you going?” he asked.", "“To the pass,” I said.", "He nodded and walked away.", "I followed him."]))[0]
        self.assertEqual(out.split("\n\n")[0], "“Where are you going?” he asked.")
        self.assertIn("“To the pass,” I said. He nodded", out)

    def test_an_abbreviation_at_a_line_end_is_not_a_sentence_end(self):
        out = op.prepass_text("\n".join(["He met Colonel F. M.", "“Bailey” in Kashgar.", "Mr.", "“Smith” left."]))[0]
        self.assertNotIn("M.\n\n", out)
        self.assertNotIn("Mr.\n\n", out)

    def test_blocks_are_never_split_mid_sentence_and_never_grow_without_bound(self):
        sentence = "The caravan moved slowly over the endless plain and nobody said very much at all."
        lines = [sentence for _ in range(120)]
        out, report = op.prepass_text("\n".join(lines))
        blocks = out.strip().split("\n\n")
        self.assertGreater(len(blocks), 3)
        self.assertTrue(all(b.endswith(".") for b in blocks))
        self.assertLess(max(len(b) for b in blocks), 2 * op.SOFT_BLOCK_CHARS)
        self.assertEqual(report.forced_splits, len(blocks) - 1)

    def test_hard_wrapped_text_breaks_paragraphs_at_short_last_lines(self):
        paragraph = ("It was the hope of the conspirators that the forces would come to their assistance as soon as "
                     "the roads were open and the weather allowed it, and they waited patiently for the news.")
        lines = []
        for _ in range(8):
            lines += textwrap.wrap(paragraph, width=60)
        out, report = op.prepass_text("\n".join(lines))
        self.assertTrue(report.runs[0][2], report.runs)
        self.assertEqual(len(out.strip().split("\n\n")), 8, out)


class SafetyTests(unittest.TestCase):
    def test_no_body_words_are_lost(self):
        pages = [(None, ["CHAPTER", "I.", "A TITLE."] + BODY)] + [("THE BOOK" if i % 2 else "A TITLE", BODY) for i in range(5)]
        out, _ = op.prepass_text(book(pages))
        for word in set(words(" ".join(BODY))):
            self.assertEqual(words(out).count(word), words(" ".join(BODY)).count(word) * 6, word)

    def test_running_it_again_changes_nothing(self):
        pages = [(None, ["CHAPTER", "I.", "A TITLE."] + ["“Quote one.”", "“Quote two.”"] + BODY)] + [("THE BOOK" if i % 2 else "A TITLE", BODY) for i in range(5)]
        once, _ = op.prepass_text(book(pages) + "\nThe discom-\nfort grew.")
        twice, report = op.prepass_text(once)
        self.assertEqual(once, twice)
        self.assertTrue(report.already_prepassed)

    def test_crlf_bom_and_nbsp_are_normalised(self):
        out, _ = op.prepass_text("\ufeffThe\u00a0caravan left.\r\nIt was early.\r\n")
        self.assertEqual(out, "The caravan left. It was early.\n")


class FileAndCliTests(GuardedCase):
    def test_prepass_file_writes_lf_utf8_and_leaves_the_input_alone(self):
        with Workspace() as ws:
            src = ws.books_dir / "in.txt"
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_bytes("Una línea\r\nmás texto.\r\n".encode("utf-8"))
            dst = ws.books_dir / "sub" / "out.txt"
            op.prepass_file(src, dst)
            self.assertEqual(src.read_bytes(), "Una línea\r\nmás texto.\r\n".encode("utf-8"))
            self.assertEqual(dst.read_bytes(), "Una línea más texto.\n".encode("utf-8"))

    def test_cli_writes_to_s1b_prepass_records_nothing_in_the_ledger_and_s2_still_sees_one_source(self):
        with Workspace() as ws:
            root = make_book(ws, "pre")
            source = root / "s1b" / "source" / "tiny_source.txt"
            before = source.read_bytes()
            code, output = run_cli(ws, "s1b", "prepass", source, "--report", example=True)
            self.assertEqual(code, 0, output)
            target = root / "s1b" / "prepass" / "tiny_source.txt"
            self.assertTrue(target.exists(), output)
            self.assertTrue((root / "s1b" / "prepass" / "tiny_source.txt.report.txt").exists())
            self.assertIn("CHAPTER I\nTHE ROAD TO KASHGAR", target.read_text(encoding="utf-8"))
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(ledger(root).get("tasks", {}), {})
            found, reason = orchestrator._source_file(root)
            self.assertEqual(found, source, reason)
            self.assertIn("Next: python pipeline.py s1b cleanup", output)

    def test_cli_refuses_to_overwrite_its_input_and_needs_a_book_or_an_output(self):
        with Workspace() as ws:
            root = make_book(ws, "pre")
            source = root / "s1b" / "source" / "tiny_source.txt"
            code, output = run_cli(ws, "s1b", "prepass", source, "--output", source, example=True)
            self.assertNotEqual(code, 0)
            self.assertIn("overwrite", output)
            loose = ws.books_dir / "loose.txt"
            loose.write_text("Some text.\n", encoding="utf-8")
            code, output = run_cli(ws, "s1b", "prepass", loose, example=True)
            self.assertNotEqual(code, 0)
            self.assertIn("book root", output)
            code, output = run_cli(ws, "s1b", "prepass", loose, "--output", ws.books_dir / "out.txt", example=True)
            self.assertEqual(code, 0, output)


if __name__ == "__main__":
    unittest.main()
