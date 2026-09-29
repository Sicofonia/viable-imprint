"""ADR 021 — shared OpenAI-wire base, Mistral as a subclass, Z.ai provider,
and Z.ai's provider-aware chunk default. Stubs httpx.post and time.sleep, so
no network, no key, no quota. Run: uv run python -m unittest discover -s tests
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import click
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers import get_llm_provider  # noqa: E402
from providers.llm import openai_chat  # noqa: E402
from providers.llm.mistral import MistralProvider  # noqa: E402
from providers.llm.zai import ZaiProvider  # noqa: E402
from engines.llm_text import _resolve_default_max_chars  # noqa: E402

REQ = httpx.Request("POST", "https://example.invalid")


def ok(text="hola", finish_reason="stop"):
    return httpx.Response(200, request=REQ, json={
        "choices": [{"finish_reason": finish_reason, "message": {"content": text}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })


def zai_error(status, code, message="The service may be temporarily overloaded"):
    return httpx.Response(status, request=REQ, json={"error": {"code": code, "message": message}})


class Net:
    """Scripted responses (last one repeats); records every call's kwargs."""

    def __init__(self, *items):
        self.items, self.calls = list(items), []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, Exception):
            raise item
        return item


def run(provider, net):
    with mock.patch.object(openai_chat.httpx, "post", net.post), \
         mock.patch.object(openai_chat.time, "sleep"), \
         mock.patch.object(openai_chat.click, "echo"):
        return provider.complete("sys", "user")


class ZaiProviderTests(unittest.TestCase):
    def provider(self, **kwargs):
        return ZaiProvider("KEY", "glm-4.7-flash", **kwargs)

    def test_request_shape(self):
        net = Net(ok())
        self.assertEqual(run(self.provider(), net), "hola")
        call = net.calls[0]
        self.assertEqual(call["url"], "https://api.z.ai/api/paas/v4/chat/completions")
        self.assertEqual(call["timeout"], 300.0)  # ADR 021 Decision 4, not Mistral's 120
        self.assertEqual(call["headers"]["Authorization"], "Bearer KEY")
        self.assertEqual(call["json"]["model"], "glm-4.7-flash")
        self.assertEqual(call["json"]["thinking"], {"type": "disabled"})

    def test_thinking_can_be_enabled(self):
        net = Net(ok())
        run(self.provider(thinking="enabled"), net)
        self.assertEqual(net.calls[0]["json"]["thinking"], {"type": "enabled"})

    def test_overload_429_is_retried_then_succeeds(self):
        net = Net(zai_error(429, "1305"), ok("bien"))
        self.assertEqual(run(self.provider(), net), "bien")
        self.assertEqual(len(net.calls), 2)

    def test_exhausted_429_names_code_and_hint(self):
        with self.assertRaises(click.ClickException) as ctx:
            run(self.provider(), Net(zai_error(429, "1305")))
        message = ctx.exception.message
        self.assertIn("(code 1305)", message)
        self.assertIn("z.ai/manage-apikey/rate-limits", message)

    def test_non_retryable_error_keeps_code(self):
        net = Net(zai_error(400, "1211", "Unknown Model, please check the model code."))
        with self.assertRaises(click.ClickException) as ctx:
            run(self.provider(), net)
        self.assertIn("Unknown Model", ctx.exception.message)
        self.assertIn("(code 1211)", ctx.exception.message)
        self.assertEqual(len(net.calls), 1)

    def test_truncated_reply_fails_without_retry(self):
        net = Net(ok("medio texto", finish_reason="length"))
        with self.assertRaises(click.ClickException) as ctx:
            run(self.provider(), net)
        self.assertIn("finish_reason='length'", ctx.exception.message)
        self.assertIn("max_chars", ctx.exception.message)
        self.assertEqual(len(net.calls), 1)

    def test_other_finish_reason_fails(self):
        with self.assertRaises(click.ClickException) as ctx:
            run(self.provider(), Net(ok("x", finish_reason="sensitive")))
        self.assertIn("finish_reason='sensitive'", ctx.exception.message)

    def test_empty_reply_fails(self):
        with self.assertRaises(click.ClickException):
            run(self.provider(), Net(ok("   ")))

    def test_usage_accumulates_across_calls(self):
        provider, net = self.provider(), Net(ok(), ok())
        run(provider, net)
        run(provider, net)
        self.assertEqual(provider.usage, {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30})


class MistralSubclassTests(unittest.TestCase):
    def test_request_shape_unchanged(self):
        net = Net(ok())
        run(MistralProvider("KEY", "mistral-medium-latest"), net)
        call = net.calls[0]
        self.assertEqual(call["url"], "https://api.mistral.ai/v1/chat/completions")
        self.assertEqual(call["timeout"], 120.0)
        self.assertNotIn("thinking", call["json"])

    def test_default_model_kept(self):
        self.assertEqual(MistralProvider("KEY")._model, "mistral-medium-latest")

    def test_error_message_prefix(self):
        with self.assertRaises(click.ClickException) as ctx:
            run(MistralProvider("KEY"), Net(httpx.Response(400, request=REQ, json={"message": "bad"})))
        self.assertEqual(ctx.exception.message, "Mistral request failed: 400 — bad")


class ChunkDefaultTests(unittest.TestCase):
    def test_zai_default(self):
        self.assertEqual(_resolve_default_max_chars({"llm": {"provider": "z-ai"}}), 6000)

    def test_zai_configured(self):
        config = {"llm": {"provider": "z-ai", "limits": {"max_context_chars": 9000}}}
        self.assertEqual(_resolve_default_max_chars(config), 9000)

    def test_zai_ignores_output_ceiling(self):
        config = {"llm": {"provider": "z-ai", "limits": {"max_output_tokens": 65536}}}
        self.assertEqual(_resolve_default_max_chars(config), 6000)

    def test_google_unchanged(self):
        config = {"llm": {"provider": "google-aistudio", "limits": {"max_output_tokens": 65536}}}
        self.assertEqual(_resolve_default_max_chars(config), int(65536 * 3.5))

    def test_mistral_unchanged(self):
        self.assertEqual(_resolve_default_max_chars({"llm": {"provider": "mistral"}}), 8000)


class DispatchTests(unittest.TestCase):
    def config(self, **llm):
        return {"llm": {"provider": "z-ai", "model": "glm-4.7-flash", **llm}}

    def test_builds_zai(self):
        with mock.patch.dict(os.environ, {"ZAI_API_KEY": "k"}):
            provider = get_llm_provider(self.config())
        self.assertIsInstance(provider, ZaiProvider)
        self.assertEqual(provider._thinking, "disabled")

    def test_missing_key(self):
        with mock.patch.dict(os.environ, {"ZAI_API_KEY": ""}):
            with self.assertRaises(click.ClickException) as ctx:
                get_llm_provider(self.config())
        self.assertIn("ZAI_API_KEY", ctx.exception.message)

    def test_bad_thinking_value(self):
        with mock.patch.dict(os.environ, {"ZAI_API_KEY": "k"}):
            with self.assertRaises(click.ClickException):
                get_llm_provider(self.config(thinking="low"))

    def test_unknown_provider_lists_zai(self):
        with self.assertRaises(ValueError) as ctx:
            get_llm_provider({"llm": {"provider": "nope", "model": "m"}})
        self.assertIn("z-ai", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
