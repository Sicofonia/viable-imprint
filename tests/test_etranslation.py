"""ADR 022 — eTranslation provider, its HTML markup conversion, provider-aware
chunk size and per-provider pricing. Stubs httpx.request and time.sleep, so no
network, no credentials, no quota. Run: uv run python -m unittest discover -s tests
"""
import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import click
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engines import translation as translation_engine  # noqa: E402
from lib import metrics  # noqa: E402
from providers import get_translation_provider  # noqa: E402
from providers.translation import etranslation  # noqa: E402
from providers.translation.deepl import DeepLProvider  # noqa: E402
from providers.translation.etranslation import ETranslationProvider, from_html, to_html  # noqa: E402

REQ = httpx.Request("GET", "https://example.invalid")
RECEIVER = "https://receiver.invalid/api/etranslation"
SECRET = "s3cret"


def resp(status, body=None):
    return httpx.Response(status, request=REQ, json=body if body is not None else {})


def delivered(html_body, request_id=123):
    page = f"<html><head><title>x</title></head><body>{html_body}</body></html>"
    return resp(200, {"status": "delivered", "delivery": {
        "requestId": request_id, "targetLanguage": "ES", "outputFormat": "html",
        "result": base64.b64encode(page.encode()).decode()}})


PENDING = resp(200, {"status": "pending"})
STATUS_OK = resp(200, {"level": 0})
ACCEPTED = resp(200, {"requestId": 123})


class Net:
    """Scripted responses per route (the last one repeats); records every call.
    Routes: status, submit, poll, delete. An unscripted route fails the test."""

    def __init__(self, **routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    def request(self, method, url, **kwargs):
        if url.endswith("/status"):
            route = "status"
        elif url.endswith("/askTranslate"):
            route = "submit"
        elif url.endswith("/getDomains"):
            route = "domains"
        else:
            route = "poll" if method == "GET" else "delete"
        self.calls.append({"route": route, "method": method, "url": url, **kwargs})
        if route not in self.routes:
            raise AssertionError(f"unexpected {route} call")
        items = self.routes[route]
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, Exception):
            raise item
        return item

    def of(self, route):
        return [c for c in self.calls if c["route"] == route]


def provider(**kwargs):
    kwargs.setdefault("poll_interval_seconds", 5)
    return ETranslationProvider("app", "pw", RECEIVER, SECRET, **kwargs)


def translate(p, net, text="Hello", echoes=None):
    """Run one translate() with stubbed network; `echoes`, if given, collects
    everything the provider printed."""
    with mock.patch.object(etranslation.httpx, "request", net.request), \
         mock.patch.object(etranslation.time, "sleep") as sleep, \
         mock.patch.object(etranslation.click, "echo",
                           side_effect=(lambda msg="", **_: echoes.append(msg)) if echoes is not None else None):
        out = p.translate(text, "en", "es")
    return out, sleep


class MarkupTests(unittest.TestCase):
    SAMPLE = ("CHAPTER I\nTHE START\n\n"
              "He saw the [i]chaikhana[/i] at [sc]A.D.[/sc] 1900 & more <x>.\n\n"
              "A note [FN: see [i]Book[/i] p. 3 [/FN]] here.")

    def test_round_trip_is_lossless(self):
        self.assertEqual(from_html(to_html(self.SAMPLE)), self.SAMPLE)

    def test_to_html_shape(self):
        out = to_html(self.SAMPLE)
        for piece in ("<p>CHAPTER I<br/>THE START</p>", "<i>chaikhana</i>", '<span class="sc">A.D.</span>',
                      "&amp; more &lt;x&gt;", "[FN: see <i>Book</i> p. 3 [/FN]]"):
            self.assertIn(piece, out)

    def test_tolerates_engine_reformatting(self):
        out = from_html("<html><head><title>x</title></head><body>\n<p>One <em>two</em>\n   three</p>\n"
                        "<p>A<br>B</p>\n</body></html>")
        self.assertEqual(out, "One [i]two[/i] three\n\nA\nB")

    def test_bare_fragment_and_unknown_span(self):
        self.assertEqual(from_html("<p>uno</p><p>dos</p>"), "uno\n\ndos")
        self.assertEqual(from_html('<p>a <span class="x">b</span> <span class="sc">C</span></p>'),
                         "a b [sc]C[/sc]")

    def test_head_content_is_never_part_of_the_text(self):
        self.assertEqual(from_html("<html><head><title>NOT TEXT</title></head><body><p>Texto</p></body></html>"),
                         "Texto")

    def test_blank_blocks_are_dropped(self):
        self.assertEqual(from_html(to_html("uno\n\n\n\ndos")), "uno\n\ndos")


class SubmitTests(unittest.TestCase):
    def test_payload_shape_and_round_trip(self):
        net = Net(status=[STATUS_OK], submit=[ACCEPTED], poll=[delivered("<p>Hola <i>x</i></p><p>Adiós</p>")],
                  delete=[resp(200)])
        p = provider(document_format="html")  # the default is txt; this covers the html path
        p._reference = "book/stem"
        out, _ = translate(p, net, "Hello [i]x[/i]\n\nBye")
        self.assertEqual(out, "Hola [i]x[/i]\n\nAdiós")

        call = net.of("submit")[0]
        body = call["json"]
        self.assertEqual(call["url"], f"{etranslation.API_BASE}/askTranslate")
        self.assertIsInstance(call["auth"], httpx.BasicAuth)
        self.assertEqual((body["sourceLanguage"], body["targetLanguages"], body["domain"]), ("EN", ["ES"], "GEN"))
        self.assertEqual(body["llm"], {"enabled": False})
        self.assertEqual(body["callerInformation"], {"externalReference": "book/stem"})
        document = body["documentToTranslate"]["document"]
        self.assertEqual(document["format"], "html")
        self.assertIn("<p>Hello <i>x</i></p>", base64.b64decode(document["content"]).decode())
        self.assertEqual(body["deliveries"]["http"], f"{RECEIVER}?kind=delivery&token={SECRET}")
        self.assertEqual(body["notifications"]["success"]["http"], f"{RECEIVER}?kind=success&token={SECRET}")
        self.assertEqual(body["notifications"]["failure"]["http"], f"{RECEIVER}?kind=failure&token={SECRET}")
        self.assertEqual(p.usage["characters"], len("Hello [i]x[/i]\n\nBye"))

    def test_llm_block_can_be_omitted_and_txt_is_sent_as_is(self):
        txt = base64.b64encode("Hola".encode()).decode()
        net = Net(status=[STATUS_OK], submit=[ACCEPTED], delete=[resp(200)],
                  poll=[resp(200, {"status": "delivered", "delivery": {"requestId": 123, "result": txt,
                                                                       "outputFormat": "txt"}})])
        out, _ = translate(provider(llm_enhanced=None, document_format="txt"), net, "Hello")
        self.assertEqual(out, "Hola")
        body = net.of("submit")[0]["json"]
        self.assertNotIn("llm", body)
        self.assertEqual(base64.b64decode(body["documentToTranslate"]["document"]["content"]).decode(), "Hello")

    def test_actionable_submit_errors(self):
        cases = [(-11200, "quota"), (-20028, "50 document requests"), (-90000, "NOT_AUTHORIZED"),
                 (-10500, "llm_enhanced")]
        for code, expected in cases:
            net = Net(status=[STATUS_OK], submit=[resp(400, {"errorCode": code, "errorMessage": "M"})])
            with self.assertRaises(click.ClickException) as cm:
                translate(provider(), net)
            self.assertIn(expected, cm.exception.message, code)

    def test_unknown_error_code_is_reported_verbatim_and_bad_credentials_named(self):
        net = Net(status=[STATUS_OK], submit=[resp(400, {"errorCode": -10704, "errorMessage": "SOURCE_LANGUAGE_MISSING"})])
        with self.assertRaises(click.ClickException) as cm:
            translate(provider(), net)
        self.assertIn("SOURCE_LANGUAGE_MISSING", cm.exception.message)

        net = Net(status=[STATUS_OK], submit=[resp(401)])
        with self.assertRaises(click.ClickException) as cm:
            translate(provider(), net)
        self.assertIn("ETRANSLATION_APP_NAME", cm.exception.message)

    def test_5xx_and_network_errors_retry_then_succeed_or_give_up(self):
        net = Net(status=[STATUS_OK], submit=[resp(503), httpx.ConnectError("x"), ACCEPTED],
                  poll=[delivered("<p>Hola</p>")], delete=[resp(200)])
        out, sleep = translate(provider(), net)
        self.assertEqual(out, "Hola")
        self.assertEqual([c.args[0] for c in sleep.call_args_list][:2], [2, 4])

        net = Net(status=[STATUS_OK], submit=[resp(500)])
        with self.assertRaises(click.ClickException) as cm:
            translate(provider(), net)
        self.assertIn("3 attempts", cm.exception.message)
        self.assertEqual(len(net.of("submit")), 3)

    def test_status_level_3_stops_before_submitting(self):
        net = Net(status=[resp(200, {"level": 3})])
        with self.assertRaises(click.ClickException):
            translate(provider(), net)
        self.assertEqual(net.of("submit"), [])

    def test_unreadable_status_endpoint_does_not_stop_a_run(self):
        net = Net(status=[httpx.ConnectError("x")], submit=[ACCEPTED], poll=[delivered("<p>Hola</p>")],
                  delete=[resp(200)])
        out, _ = translate(provider(), net)
        self.assertEqual(out, "Hola")

    def test_error_messages_never_contain_the_secret(self):
        net = Net(status=[STATUS_OK], submit=[httpx.ConnectError("boom")])
        with self.assertRaises(click.ClickException) as cm:
            translate(provider(), net)
        self.assertNotIn(SECRET, cm.exception.message)


class LlmSupportWarningTests(unittest.TestCase):
    # Shaped like this account's real answer: GEN has glossary but no LLM variant.
    DOMAINS = {"GEN": {"languagePairs": ["EN-ES-GLS", "EN-FR-LLM"]},
               "SPD": {"languagePairs": ["EN-ES-LLM-GLS"]},
               "ECJ": {"languagePairs": ["EN-ES-LLM"]}}

    def run_with(self, domains_route, **kwargs):
        net = Net(status=[STATUS_OK], domains=domains_route, submit=[ACCEPTED],
                  poll=[delivered("<p>Hola</p>")], delete=[resp(200)])
        echoes = []
        p = provider(**kwargs)
        out, _ = translate(p, net, echoes=echoes)
        self.assertEqual(out, "Hola")  # a warning never stops the run
        return net, " ".join(echoes)

    def test_warns_when_the_domain_has_no_llm_variant_and_names_the_ones_that_do(self):
        net, said = self.run_with([resp(200, self.DOMAINS)], llm_enhanced=True, domain="GEN")
        self.assertIn("domain GEN has no LLM variant for EN-ES", said)
        self.assertIn("ECJ, SPD", said)
        self.assertEqual(len(net.of("submit")), 1)

    def test_silent_when_the_domain_has_an_llm_variant(self):
        for domain in ("SPD", "ECJ"):
            _, said = self.run_with([resp(200, self.DOMAINS)], llm_enhanced=True, domain=domain)
            self.assertNotIn("LLM variant", said, domain)

    def test_list_shaped_answer_is_read_too(self):
        as_list = [{"domain": d, "languagePairs": v["languagePairs"]} for d, v in self.DOMAINS.items()]
        _, said = self.run_with([resp(200, as_list)], llm_enhanced=True, domain="GEN")
        self.assertIn("no LLM variant", said)

    def test_no_extra_call_unless_llm_is_requested(self):
        for llm in (False, None):
            net = Net(status=[STATUS_OK], submit=[ACCEPTED], poll=[delivered("<p>Hola</p>")], delete=[resp(200)])
            translate(provider(llm_enhanced=llm), net)  # a getDomains call would fail the test
            self.assertEqual(net.of("domains"), [])

    def test_unreadable_domain_list_is_skipped_quietly(self):
        for broken in ([httpx.ConnectError("x")], [resp(500)], [resp(200, "not a domain list")]):
            _, said = self.run_with(broken, llm_enhanced=True, domain="GEN")
            self.assertNotIn("LLM variant", said)

    def test_checked_once_per_provider_instance(self):
        net = Net(status=[STATUS_OK], domains=[resp(200, self.DOMAINS)], submit=[ACCEPTED],
                  poll=[delivered("<p>Hola</p>")], delete=[resp(200)])
        p = provider(llm_enhanced=True, domain="GEN")
        translate(p, net, "one")
        translate(p, net, "two")
        self.assertEqual(len(net.of("domains")), 1)


class PollingTests(unittest.TestCase):
    def test_polls_through_pending_then_deletes_from_receiver(self):
        net = Net(status=[STATUS_OK], submit=[ACCEPTED], poll=[PENDING, PENDING, delivered("<p>Hola</p>")],
                  delete=[resp(200)])
        out, sleep = translate(provider(poll_interval_seconds=7), net)
        self.assertEqual(out, "Hola")
        self.assertEqual(len(net.of("poll")), 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [7, 7])
        self.assertEqual(net.of("poll")[0]["params"], {"token": SECRET, "requestId": 123})
        self.assertEqual(len(net.of("delete")), 1)

    def test_a_failed_request_is_reported_and_forgotten(self):
        failed = resp(200, {"status": "failed", "failure": {"requestId": 123, "errorCode": -30000,
                                                           "errorMessage": "Cannot convert input file"}})
        net = Net(status=[STATUS_OK], submit=[ACCEPTED], poll=[failed], delete=[resp(200)])
        p = provider()
        with self.assertRaises(click.ClickException) as cm:
            translate(p, net)
        self.assertIn("Cannot convert input file", cm.exception.message)
        self.assertEqual(p._state, {})

    def test_wrong_secret_and_wrong_url_are_named(self):
        for status, expected in ((401, "shared secret"), (404, "receiver_url")):
            net = Net(status=[STATUS_OK], submit=[ACCEPTED], poll=[resp(status)])
            with self.assertRaises(click.ClickException) as cm:
                translate(provider(), net)
            self.assertIn(expected, cm.exception.message)

    def test_timeout_keeps_the_request_for_resume(self):
        net = Net(status=[STATUS_OK], submit=[ACCEPTED], poll=[PENDING])
        with mock.patch.object(etranslation.time, "monotonic", side_effect=[0, 1, 10 ** 6, 10 ** 6]):
            with self.assertRaises(click.ClickException) as cm:
                translate(provider(timeout_minutes=1), net)
        self.assertIn("re-running resumes", cm.exception.message)

    def test_unreachable_receiver_gives_up_after_consecutive_failures(self):
        net = Net(status=[STATUS_OK], submit=[ACCEPTED], poll=[httpx.ConnectError("x")])
        with self.assertRaises(click.ClickException) as cm:
            translate(provider(), net)
        self.assertIn("unreachable", cm.exception.message)
        self.assertEqual(len(net.of("poll")), 10)

    def test_a_receiver_that_cannot_be_cleaned_does_not_fail_the_translation(self):
        net = Net(status=[STATUS_OK], submit=[ACCEPTED], poll=[delivered("<p>Hola</p>")],
                  delete=[httpx.ConnectError("x")])
        out, _ = translate(provider(), net)
        self.assertEqual(out, "Hola")


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def run_provider(self, net, text="Hello", **kwargs):
        p = provider(**kwargs)
        p.begin_run(self.dir, "stem", "book/stem")
        out, _ = translate(p, net, text)
        return p, out

    def sidecar(self):
        return self.dir / ".stem.etranslation.json"

    def test_interrupted_run_resumes_the_same_request_without_resubmitting(self):
        net = Net(status=[STATUS_OK], submit=[ACCEPTED], poll=[PENDING])
        with mock.patch.object(etranslation.time, "monotonic", side_effect=[0, 1, 10 ** 6, 10 ** 6]):
            with self.assertRaises(click.ClickException):
                self.run_provider(net, timeout_minutes=1)
        saved = json.loads(self.sidecar().read_text())["requests"]
        self.assertEqual([e["request_id"] for e in saved.values()], [123])

        net2 = Net(poll=[delivered("<p>Hola</p>")], delete=[resp(200)])  # no submit/status route
        _, out = self.run_provider(net2)
        self.assertEqual(out, "Hola")
        self.assertEqual(net2.of("submit"), [])
        self.assertEqual(net2.of("poll")[0]["params"]["requestId"], 123)

    def test_changed_text_is_not_matched_to_an_old_request(self):
        net = Net(status=[STATUS_OK], submit=[ACCEPTED], poll=[PENDING])
        with mock.patch.object(etranslation.time, "monotonic", side_effect=[0, 1, 10 ** 6, 10 ** 6]):
            with self.assertRaises(click.ClickException):
                self.run_provider(net, text="Hello", timeout_minutes=1)

        net2 = Net(status=[STATUS_OK], submit=[resp(200, {"requestId": 456})],
                   poll=[delivered("<p>Adiós</p>", 456)], delete=[resp(200)])
        _, out = self.run_provider(net2, text="Goodbye")
        self.assertEqual(out, "Adiós")
        self.assertEqual(len(net2.of("submit")), 1)

    def test_a_received_translation_survives_in_the_sidecar_with_no_network(self):
        net = Net(status=[STATUS_OK], submit=[ACCEPTED], poll=[delivered("<p>Hola</p>")], delete=[resp(200)])
        self.run_provider(net)
        self.assertTrue(self.sidecar().exists())

        _, out = self.run_provider(Net())  # any network call would fail the test
        self.assertEqual(out, "Hola")

    def test_finish_run_removes_the_sidecar(self):
        net = Net(status=[STATUS_OK], submit=[ACCEPTED], poll=[delivered("<p>Hola</p>")], delete=[resp(200)])
        p, _ = self.run_provider(net)
        p.finish_run()
        self.assertFalse(self.sidecar().exists())

    def test_unreadable_sidecar_starts_fresh(self):
        self.sidecar().write_text("{not json", encoding="utf-8")
        net = Net(status=[STATUS_OK], submit=[ACCEPTED], poll=[delivered("<p>Hola</p>")], delete=[resp(200)])
        with mock.patch.object(etranslation.click, "echo"):
            _, out = self.run_provider(net)
        self.assertEqual(out, "Hola")


class ChunkSizeAndPricingTests(unittest.TestCase):
    def test_each_provider_declares_its_own_chunk_size(self):
        self.assertEqual(DeepLProvider.max_chars_per_request, 50000)
        self.assertEqual(ETranslationProvider.max_chars_per_request, 2_000_000)

    def test_engine_uses_the_providers_chunk_size_and_runs_the_hooks(self):
        events = []

        class Fake:
            usage = {"characters": 0}
            max_chars_per_request = 20

            def begin_run(self, output_dir, stem, reference):
                events.append(("begin", Path(output_dir).name, stem, reference))

            def translate(self, text, s, t):
                events.append(("translate", text))
                return text.upper()

            def finish_run(self):
                events.append(("finish", out_file.exists()))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "s1b" / "cleaned" / "book.txt"
            src.parent.mkdir(parents=True)
            src.write_text("\n\n".join(["a" * 15] * 3), encoding="utf-8")
            out_file = root / "s1b" / "translated" / "es" / "book.txt"
            config = {"translation": {"provider": "x", "source_lang": "EN", "target_lang": "ES"}}
            with mock.patch.object(translation_engine, "get_translation_provider", return_value=Fake()), \
                    mock.patch.object(translation_engine.manifest, "update"), \
                    mock.patch.object(translation_engine.click, "echo"):
                translation_engine.run(src, root, "s1b", "translated", config)
        self.assertEqual(events[0], ("begin", "es", "book", f"{root.name}/book"))
        self.assertEqual(sum(1 for e in events if e[0] == "translate"), 3)
        self.assertEqual(events[-1], ("finish", True))  # sidecar cleared only after the output exists

    def test_a_provider_with_no_declared_size_and_no_hooks_still_works(self):
        class Plain:
            usage = {"characters": 0}

            def translate(self, text, s, t):
                return text

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "book.txt"
            src.write_text("\n\n".join(["a" * 15] * 3), encoding="utf-8")
            config = {"translation": {"provider": "x"}}
            with mock.patch.object(translation_engine, "get_translation_provider", return_value=Plain()), \
                    mock.patch.object(translation_engine.manifest, "update"), \
                    mock.patch.object(translation_engine.click, "echo") as echo:
                translation_engine.run(src, root, "s1b", "translated", config)
        self.assertIn("chunk 1/1", " ".join(str(c.args[0]) for c in echo.call_args_list))

    def test_pricing_is_per_provider(self):
        usage = {"usage": {"characters": 1_000_000}}

        def cost(translation):
            return metrics.enrich(usage, {"translation": translation})["cost_usd"]

        legacy = {"per_million_characters": 20.0}
        self.assertEqual(cost({"provider": "etranslation", "etranslation": {"pricing": {"per_million_characters": 0.0}},
                               "pricing": legacy}), 0.0)
        self.assertIsNone(cost({"provider": "etranslation", "pricing": legacy}))  # legacy never applies to it
        self.assertEqual(cost({"provider": "deepl", "pricing": legacy}), 20.0)    # legacy still honored for DeepL
        self.assertEqual(cost({"provider": "deepl", "pricing": legacy,
                               "deepl": {"pricing": {"per_million_characters": 5.0}}}), 5.0)
        self.assertIsNone(cost({"provider": "etranslation"}))


class FactoryTests(unittest.TestCase):
    ENV = {"ETRANSLATION_APP_NAME": "app", "ETRANSLATION_PASSWORD": "pw", "ETRANSLATION_RECEIVER_SECRET": SECRET}

    def make(self, settings=None, env=None, **extra):
        config = {"translation": {"provider": "etranslation",
                                  "etranslation": {"receiver_url": RECEIVER, **(settings or {})}, **extra}}
        with mock.patch.dict(os.environ, self.ENV if env is None else env, clear=True):
            return get_translation_provider(config)

    def test_builds_a_configured_provider(self):
        p = self.make({"domain": "SPD", "document_format": "txt", "llm_enhanced": None,
                       "poll_interval_seconds": 10, "timeout_minutes": 5})
        self.assertEqual((p.domain, p.document_format, p.llm_enhanced, p.poll_interval_seconds, p.timeout_minutes),
                         ("SPD", "txt", None, 10.0, 5.0))
        defaults = self.make()
        self.assertEqual((defaults.domain, defaults.document_format, defaults.llm_enhanced), ("GEN", "txt", False))

    def test_missing_credentials_are_all_named(self):
        with self.assertRaises(click.ClickException) as cm:
            self.make(env={})
        for key in self.ENV:
            self.assertIn(key, cm.exception.message)

    def test_bad_settings_fail_with_a_message(self):
        for bad in ({"receiver_url": "http://x/api"}, {"receiver_url": RECEIVER + "?a=1"},
                    {"receiver_url": None}, {"document_format": "docx"}, {"llm_enhanced": "yes"},
                    {"poll_interval_seconds": 0}, {"timeout_minutes": True}):
            with self.assertRaises(click.ClickException, msg=bad):
                self.make(bad)

    def test_unknown_provider_lists_both(self):
        with self.assertRaises(ValueError) as cm:
            get_translation_provider({"translation": {"provider": "nope"}})
        self.assertIn("etranslation, deepl", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
