"""ADR 023, T1.4 — services/estimate.py: provider, input size, approximate calls and
cost before a run. Expected numbers are worked out by hand from the fixtures and
the fixed test pricing, not by calling the code under test. No Textual needed.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402
from harness import GuardedCase, PRICING_LLM, PRICING_TRANSLATION, REPO_ROOT, Workspace, fixture, make_book  # noqa: E402
from lib import paths  # noqa: E402
from lib.chunker import chunk_by_paragraphs  # noqa: E402
from tui.services import estimate  # noqa: E402
from tui.services.context import Context  # noqa: E402


def paragraphs(count: int, size: int) -> str:
    return "\n\n".join(("p%03d " % i + "x" * (size - 5)) for i in range(count))


class EstimateCase(GuardedCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path)
        manifests = harness.example_manifests()  # example prompts exist on a clean clone
        manifests.__enter__()
        self.addCleanup(manifests.__exit__, None, None, None)

    def write(self, name: str, text: str) -> Path:
        path = self.ws.base / name
        path.write_text(text, encoding="utf-8")
        return path


class LlmEstimateTests(EstimateCase):
    def test_one_chunk_matches_a_hand_calculation(self):
        source = fixture("tiny_source.txt")
        text_chars = len(source.read_text(encoding="utf-8"))
        prompt_chars = len(paths.load_prompt("prompts/examples/s1b/cleanup_task.txt"))
        prompt_tokens, completion_tokens = (prompt_chars + text_chars) // 4, text_chars // 4
        expected_cost = round(prompt_tokens / 1e6 * PRICING_LLM["prompt_per_million"]
                              + completion_tokens / 1e6 * PRICING_LLM["completion_per_million"], 6)

        result = estimate.estimate_command(self.ctx, ["s1b", "cleanup"], {"input_file": str(source)})

        self.assertEqual((result.kind, result.provider, result.model), ("llm", "fake", "fake-1"))
        self.assertEqual((result.input_chars, result.calls), (text_chars, 1))
        self.assertEqual(result.cost_usd, expected_cost)
        lines = result.lines()
        self.assertEqual(lines[0], "Provider: fake / fake-1")
        self.assertEqual(lines[1], f"Input: {text_chars:,} characters")
        self.assertEqual(lines[2], "Provider calls: about 1")
        self.assertTrue(lines[3].startswith("Estimated cost: $"), lines[3])

    def test_no_pricing_says_so(self):
        del self.ws.config["llm"]["pricing"]
        self.ws.write_config()
        self.ctx.reload_config()
        result = estimate.estimate_command(self.ctx, ["s1b", "cleanup"], {"input_file": str(fixture("tiny_source.txt"))})
        self.assertIsNone(result.cost_usd)
        self.assertFalse(result.pricing_configured)
        self.assertEqual(result.lines()[-1], "Estimated cost: no pricing configured")

    def test_calls_follow_the_engines_chunking(self):
        text = paragraphs(30, 1000)  # about 30,000 characters; the fake provider's default chunk is 8,000
        path = self.write("long.txt", text)
        result = estimate.estimate_command(self.ctx, ["s1b", "cleanup"], {"input_file": str(path)})
        self.assertEqual(result.calls, len(chunk_by_paragraphs(text, 8000)))
        self.assertGreater(result.calls, 1)

    def test_a_task_max_chars_wins_and_a_reduce_prompt_adds_a_call(self):
        text = paragraphs(40, 4000)  # synopsis: max_chars 100000, so two chunks and a reduce call
        path = self.write("brief.txt", text)
        result = estimate.estimate_command(self.ctx, ["s1d", "synopsis"], {"input_file": str(path)})
        self.assertEqual(result.calls, len(chunk_by_paragraphs(text, 100000)) + 1)
        self.assertEqual(result.calls, 3)

    def test_an_input_that_cannot_be_split_is_reported_before_any_call(self):
        path = self.write("scan.txt", paragraphs(40, 4000))  # s4 briefing: single_chunk, no reduce prompt
        result = estimate.estimate_command(self.ctx, ["s4", "briefing"], {"input_file": str(path)})
        self.assertEqual(result.calls, 0)
        self.assertIn("will stop before calling the provider", result.notes[0])

    def test_no_estimate_for_zero_provider_actions(self):
        csv = str(fixture("tiny_kdp.csv"))
        for path, values in ((["s1d", "metadata"], {"input_file": csv}), (["s4", "scan"], {}),
                             (["s3", "sales-ingest"], {"book_slug": "x", "csv_file": csv}),
                             (["s3", "dashboard"], {}), (["s1c", "check"], {"input_file": csv}),
                             (["s1b", "format"], {"input_file": csv}), (["s5", "homeostat-scan"], {}),
                             (["book", "set-isbn"], {}), (["init"], {})):
            self.assertIsNone(estimate.estimate_command(self.ctx, path, values), " ".join(path))

    def test_a_missing_input_still_counts_one_call(self):
        result = estimate.estimate_command(self.ctx, ["s1b", "cleanup"], {"input_file": str(self.ws.base / "nope.txt")})
        self.assertEqual((result.calls, result.input_chars), (1, None))
        self.assertIn("Input: size not known yet", result.lines())


class TranslationEstimateTests(EstimateCase):
    def test_chunked_translation_uses_the_provider_default_limit_and_pricing(self):
        text = paragraphs(60, 2000)  # 120,000 characters, over two 50,000-character requests
        path = self.write("cleaned.txt", text)
        result = estimate.estimate_command(self.ctx, ["s1b", "translate"], {"input_file": str(path)})
        self.assertEqual((result.kind, result.provider, result.input_chars), ("translation", "fake", len(text)))
        self.assertEqual(result.calls, 3)
        self.assertAlmostEqual(result.cost_usd, len(text) / 1e6 * PRICING_TRANSLATION["per_million_characters"], places=6)

    def test_document_mode_is_one_request(self):
        path = self.write("cleaned.txt", paragraphs(60, 2000))
        result = estimate.estimate_command(self.ctx, ["s1b", "translate"], {"input_file": str(path), "document": True})
        self.assertEqual(result.calls, 1)

    def test_etranslation_states_the_wait_and_the_resume_behaviour(self):
        self.ws.update("translation", provider="etranslation")
        self.ctx.reload_config()
        path = self.write("cleaned.txt", paragraphs(60, 2000))
        result = estimate.estimate_command(self.ctx, ["s1b", "translate"], {"input_file": str(path)})
        self.assertEqual(result.calls, 1)  # eTranslation accepts 2,000,000 characters per request
        self.assertIsNone(result.cost_usd)
        self.assertTrue(any("two hours" in n and "resumes" in n for n in result.notes), result.notes)


class ChainEstimateTests(EstimateCase):
    def test_book_run_covers_what_is_ready_now(self):
        make_book(self.ws, "tiny")
        result = estimate.estimate_command(self.ctx, ["s2", "run"], {"book_slug": "tiny"})
        self.assertEqual(result.kind, "chain")
        self.assertEqual([i.label for i in result.items], ["s1b cleanup"])
        self.assertEqual(result.calls, 1)
        self.assertTrue(any("More tasks become ready" in n for n in result.notes))
        self.assertIn("- s1b cleanup: 1 call", "\n".join(result.lines()))

    def test_book_run_step_has_no_more_to_come_note_and_nothing_ready_is_no_estimate(self):
        make_book(self.ws, "tiny")
        step = estimate.estimate_command(self.ctx, ["s2", "run"], {"book_slug": "tiny", "step": True})
        self.assertFalse(any("More tasks" in n for n in step.notes))
        self.assertIsNone(estimate.estimate_command(self.ctx, ["s2", "run"], {"book_slug": "tiny", "only": "s1d"}))

    def test_periodic_chains_name_their_llm_stage(self):
        homeostat = estimate.estimate_command(self.ctx, ["s2", "homeostat", "run"], {})
        self.assertEqual([i.label for i in homeostat.items], ["s5 homeostat"])
        newsletter = estimate.estimate_command(self.ctx, ["s2", "newsletter", "run"], {})
        self.assertEqual([i.label for i in newsletter.items], ["s1d newsletter"])
        self.assertTrue(any("not known yet" in n for n in newsletter.notes))

    def test_periodic_step_runs_only_the_scan_which_calls_no_provider(self):
        self.assertIsNone(estimate.estimate_command(self.ctx, ["s2", "homeostat", "run"], {"step": True}))


if __name__ == "__main__":
    unittest.main()
