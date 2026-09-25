"""ADR 020 — model fallback on sustained 503. Stubs httpx.post and time.sleep,
so no network, no quota. Run: uv run python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import click
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.llm import google_aistudio as gas  # noqa: E402
from engines.llm_text import _served_models  # noqa: E402
from lib import metrics  # noqa: E402

PRIMARY, FB1, FB2 = "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.5-flash"


def _resp(status, text="ok", headers=None):
    request = httpx.Request("POST", "https://example.invalid")
    if status == 200:
        body = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}}
    else:
        body = {"error": {"code": status, "message": "high demand", "status": "UNAVAILABLE"}}
    return httpx.Response(status, json=body, headers=headers or {}, request=request)


class FakeNetwork:
    """Scripted per-model responses: {model: [response-or-exception, ...]};
    the last item repeats forever. Records every model requested, in order."""

    def __init__(self, script):
        self.script, self.calls = script, []

    def post(self, url, **kwargs):
        model = url.split("/models/")[1].split(":")[0]
        self.calls.append(model)
        queue = self.script[model]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


class FallbackTests(unittest.TestCase):
    def run_complete(self, script, calls=1, **provider_kwargs):
        net = FakeNetwork(script)
        provider = gas.GoogleAIStudioProvider("key", PRIMARY, **provider_kwargs)
        sleeps = []
        with mock.patch.object(gas.httpx, "post", net.post), \
             mock.patch.object(gas.time, "sleep", sleeps.append), \
             mock.patch.object(gas.click, "echo"):
            results = [provider.complete("sys", "user") for _ in range(calls)]
        return provider, net, sleeps, results

    def test_no_fallback_configured_is_unchanged(self):
        provider, net, _, _ = self.run_complete({PRIMARY: [_resp(200)]})
        self.assertEqual(net.calls, [PRIMARY])
        self.assertEqual(provider.models_used, [PRIMARY])

    def test_brief_503_recovers_on_same_model(self):
        provider, net, _, _ = self.run_complete(
            {PRIMARY: [_resp(503), _resp(503), _resp(200)], FB1: [_resp(200)]},
            fallback_models=[FB1])
        self.assertEqual(net.calls, [PRIMARY] * 3)
        self.assertEqual(provider.models_used, [PRIMARY])

    def test_sustained_503_switches_to_fallback(self):
        provider, net, sleeps, results = self.run_complete(
            {PRIMARY: [_resp(503)], FB1: [_resp(200, "from-fb1")]}, fallback_models=[FB1])
        self.assertEqual(results, ["from-fb1"])
        # backoff 2+4+8+16+32 = 62s >= 60s window, then switch with no extra wait
        self.assertEqual(sleeps, [2, 4, 8, 16, 32])
        self.assertEqual(net.calls, [PRIMARY] * 6 + [FB1])
        self.assertEqual(provider.models_used, [FB1])
        self.assertEqual(provider.model, FB1)

    def test_sticky_across_calls(self):
        provider, net, _, _ = self.run_complete(
            {PRIMARY: [_resp(503)], FB1: [_resp(200)]}, calls=3, fallback_models=[FB1])
        # primary is only ever tried during the first call
        self.assertEqual(net.calls.count(PRIMARY), 6)
        self.assertEqual(net.calls[-2:], [FB1, FB1])

    def test_mixed_models_recorded_in_order(self):
        script = {PRIMARY: [_resp(200), _resp(503)], FB1: [_resp(200)]}
        provider, _, _, _ = self.run_complete(script, calls=2, fallback_models=[FB1])
        self.assertEqual(provider.models_used, [PRIMARY, FB1])

    def test_all_models_exhaust_shared_budget_and_error_names_them(self):
        net = FakeNetwork({PRIMARY: [_resp(503)], FB1: [_resp(503)], FB2: [_resp(503)]})
        provider = gas.GoogleAIStudioProvider("key", PRIMARY, fallback_models=[FB1, FB2])
        slept = []
        with mock.patch.object(gas.httpx, "post", net.post), \
             mock.patch.object(gas.time, "sleep", slept.append), \
             mock.patch.object(gas.click, "echo"):
            with self.assertRaises(click.ClickException) as ctx:
                provider.complete("sys", "user")
        message = str(ctx.exception.message)
        for model in (PRIMARY, FB1, FB2):
            self.assertIn(model, message)
        self.assertLessEqual(sum(slept), gas._MAX_RETRY_SECONDS)  # one budget across models

    def test_read_timeout_does_not_switch(self):
        timeout = httpx.ReadTimeout("slow")
        net = FakeNetwork({PRIMARY: [timeout, timeout, _resp(200)], FB1: [_resp(200)]})
        provider = gas.GoogleAIStudioProvider("key", PRIMARY, fallback_models=[FB1], fallback_after_seconds=0)
        with mock.patch.object(gas.httpx, "post", net.post), \
             mock.patch.object(gas.time, "sleep"), mock.patch.object(gas.click, "echo"):
            provider.complete("sys", "user")
        self.assertEqual(net.calls, [PRIMARY] * 3)

    def test_429_does_not_switch(self):
        net = FakeNetwork({PRIMARY: [_resp(429), _resp(200)], FB1: [_resp(200)]})
        provider = gas.GoogleAIStudioProvider("key", PRIMARY, fallback_models=[FB1], fallback_after_seconds=0)
        with mock.patch.object(gas.httpx, "post", net.post), \
             mock.patch.object(gas.time, "sleep"), mock.patch.object(gas.click, "echo"):
            provider.complete("sys", "user")
        self.assertEqual(net.calls, [PRIMARY] * 2)

    def test_interleaved_non_503_resets_the_window(self):
        # 503s separated by a timeout are not "continuous" — window restarts
        timeout = httpx.ReadTimeout("slow")
        script = {PRIMARY: [_resp(503), _resp(503), _resp(503), _resp(503), timeout,
                            _resp(503), _resp(200)], FB1: [_resp(200)]}
        provider, net, _, _ = self.run_complete(script, fallback_models=[FB1])
        self.assertNotIn(FB1, net.calls)

    def test_duplicate_and_primary_entries_ignored(self):
        provider = gas.GoogleAIStudioProvider("key", PRIMARY, fallback_models=[PRIMARY, FB1, FB1])
        self.assertEqual(provider._models, [PRIMARY, FB1])


class LedgerTests(unittest.TestCase):
    CONFIG = {"llm": {"provider": "google-aistudio", "model": PRIMARY,
                      "pricing": {"prompt_per_million": 1, "completion_per_million": 1}}}
    USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    def test_no_switch_ledger_shape_unchanged(self):
        llm = mock.Mock(models_used=[PRIMARY])
        model, used = _served_models(llm, self.CONFIG)
        self.assertEqual((model, used), (PRIMARY, None))
        fields = metrics.enrich({"usage": self.USAGE, "model": model}, self.CONFIG)
        self.assertEqual(fields["model"], PRIMARY)
        self.assertNotIn("models_used", fields)

    def test_provider_without_models_used_unchanged(self):  # e.g. Mistral
        self.assertEqual(_served_models(object(), self.CONFIG), (PRIMARY, None))

    def test_switch_records_served_models(self):
        llm = mock.Mock(models_used=[PRIMARY, FB1])
        model, used = _served_models(llm, self.CONFIG)
        fields = metrics.enrich({"usage": self.USAGE, "model": model, "models_used": used}, self.CONFIG)
        self.assertEqual(fields["model"], FB1)
        self.assertEqual(fields["models_used"], [PRIMARY, FB1])

    def test_switch_before_any_primary_success_still_flagged(self):
        model, used = _served_models(mock.Mock(models_used=[FB1]), self.CONFIG)
        self.assertEqual((model, used), (FB1, [FB1]))


if __name__ == "__main__":
    unittest.main()
