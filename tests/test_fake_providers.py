"""ADR 023, T0.2 — the fake LLM and translation providers. No network, no key,
no quota. Run: uv run python -m unittest discover -s tests
"""
import os
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import metrics  # noqa: E402
from providers import get_llm_provider, get_translation_provider  # noqa: E402
from providers.llm.fake import FakeLLMProvider  # noqa: E402
from providers.translation.fake import FakeTranslationProvider  # noqa: E402

CONFIG = {
    "llm": {
        "provider": "fake", "model": "fake-1",
        "pricing": {"prompt_per_million": 2.0, "completion_per_million": 10.0},
    },
    "translation": {
        "provider": "fake",
        "fake": {"pricing": {"per_million_characters": 20.0}},
    },
}

TEXT = "CHAPTER I.\nThe caravan left before dawn. " * 12  # longer than the 200-char excerpt


class FakeLLMTests(unittest.TestCase):
    def test_same_input_gives_same_output(self):
        a, b = FakeLLMProvider(), FakeLLMProvider()
        self.assertEqual(a.complete("sys", TEXT), b.complete("sys", TEXT))
        self.assertNotEqual(a.complete("sys", TEXT), a.complete("sys", "Another opening. " + TEXT))

    def test_every_structural_tag_is_present(self):
        out = FakeLLMProvider().complete("sys", TEXT)
        for pattern in (r"<overview>.+?</overview>", r"<explorer>.+?</explorer>",
                        r"<dish>.+?</dish>", r"<verdict>.+?</verdict>", r"^## ."):
            self.assertRegex(out, re.compile(pattern, re.DOTALL | re.MULTILINE))

    def test_includes_first_200_characters_of_the_input(self):
        self.assertIn(TEXT[:200], FakeLLMProvider().complete("sys", TEXT))

    def test_angle_brackets_in_the_excerpt_cannot_open_a_second_tag(self):
        out = FakeLLMProvider().complete("sys", "<overview>already a tag</overview>")
        self.assertEqual(out.count("<overview>"), 1)

    def test_usage_accumulates_as_characters_over_four(self):
        llm = FakeLLMProvider()
        first = llm.complete("abcd", "efgh" * 10)  # (4 + 40) // 4 = 11 prompt tokens
        self.assertEqual(llm.usage["prompt_tokens"], 11)
        self.assertEqual(llm.usage["completion_tokens"], len(first) // 4)
        llm.complete("abcd", "efgh" * 10)
        self.assertEqual(llm.usage["prompt_tokens"], 22)
        self.assertEqual(llm.usage["total_tokens"], llm.usage["prompt_tokens"] + llm.usage["completion_tokens"])

    def test_metrics_enrich_computes_cost_from_the_test_pricing_block(self):
        llm = get_llm_provider(CONFIG)
        llm.complete("s" * 400, "u" * 400)
        fields = metrics.enrich({"usage": llm.usage, "model": "fake-1"}, CONFIG)
        self.assertEqual((fields["provider"], fields["model"]), ("fake", "fake-1"))
        expected = round(llm.usage["prompt_tokens"] / 1e6 * 2.0 + llm.usage["completion_tokens"] / 1e6 * 10.0, 6)
        self.assertEqual(fields["cost_usd"], expected)
        self.assertGreater(fields["cost_usd"], 0)

    def test_fail_raises(self):
        with self.assertRaises(RuntimeError):
            FakeLLMProvider(fail=True).complete("sys", "x")

    def test_delay_seconds_delays(self):
        start = time.monotonic()
        FakeLLMProvider(delay_seconds=0.2).complete("sys", "x")
        self.assertGreaterEqual(time.monotonic() - start, 0.19)


class FakeTranslationTests(unittest.TestCase):
    def test_returns_input_unchanged_and_preserves_markup(self):
        text = "El [i]Tigris[/i] y los [sc]A.D.[/sc] fluyen.\n[FN: nota al pie [/FN]]"
        self.assertEqual(FakeTranslationProvider().translate(text, "EN", "ES"), text)

    def test_usage_accumulates_characters(self):
        t = FakeTranslationProvider()
        t.translate("abcde", "EN", "ES")
        t.translate("fg", "EN", "ES")
        self.assertEqual(t.usage, {"characters": 7})

    def test_metrics_enrich_uses_the_nested_provider_pricing(self):
        t = get_translation_provider(CONFIG)
        t.translate("x" * 1_000_000, "EN", "ES")
        fields = metrics.enrich({"usage": t.usage}, CONFIG)
        self.assertEqual((fields["provider"], fields["cost_usd"]), ("fake", 20.0))

    def test_declares_no_optional_capabilities(self):
        t = FakeTranslationProvider()
        for name in ("translate_document", "begin_run", "finish_run", "max_chars_per_request"):
            self.assertIsNone(getattr(t, name, None), name)

    def test_fail_raises_and_delay_delays(self):
        with self.assertRaises(RuntimeError):
            FakeTranslationProvider(fail=True).translate("x", "EN", "ES")
        start = time.monotonic()
        FakeTranslationProvider(delay_seconds=0.2).translate("x", "EN", "ES")
        self.assertGreaterEqual(time.monotonic() - start, 0.19)


class SelectionTests(unittest.TestCase):
    def test_selecting_fake_needs_no_environment_variable(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsInstance(get_llm_provider(CONFIG), FakeLLMProvider)
            self.assertIsInstance(get_translation_provider(CONFIG), FakeTranslationProvider)

    def test_config_keys_reach_the_providers(self):
        config = {"llm": {"provider": "fake", "model": "m", "fail": True, "delay_seconds": 3},
                  "translation": {"provider": "fake", "fail": True, "delay_seconds": 2}}
        llm, tr = get_llm_provider(config), get_translation_provider(config)
        self.assertEqual((llm.fail, llm.delay_seconds, tr.fail, tr.delay_seconds), (True, 3.0, True, 2.0))

    def test_bad_option_values_are_rejected(self):
        for key, value in (("fail", "yes"), ("delay_seconds", -1), ("delay_seconds", True)):
            with self.assertRaises(click.ClickException):
                get_llm_provider({"llm": {"provider": "fake", "model": "m", key: value}})

    def test_model_is_still_required(self):
        with self.assertRaises(click.ClickException):
            get_llm_provider({"llm": {"provider": "fake"}})


class NoNetworkTests(unittest.TestCase):
    def test_nothing_touches_the_network(self):
        import deepl
        import httpx

        def boom(*args, **kwargs):
            raise AssertionError("network touched")

        with mock.patch.object(httpx, "post", boom), mock.patch.object(httpx, "get", boom), \
                mock.patch.object(deepl, "Translator", boom):
            get_llm_provider(CONFIG).complete("sys", TEXT)
            get_translation_provider(CONFIG).translate(TEXT, "EN", "ES")


if __name__ == "__main__":
    unittest.main()
