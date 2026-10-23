"""Deterministic translation provider for tests (ADR 023, T0.2). Returns its
input unchanged, so `[i]...[/i]`, `[sc]...[/sc]` and `[FN: ...]` markup is
preserved trivially. Selected with `translation.provider: fake`; no network,
no key.

Deliberately declares none of the optional capabilities (`translate_document`,
`begin_run`, `finish_run`, `max_chars_per_request`), so the engine takes the
same plain chunked path it takes for DeepL.

Config keys, both optional, read under `translation:`:
- `fail: true` makes every call raise.
- `delay_seconds: N` sleeps N seconds before answering.
"""
import time

from providers.translation.base import TranslationProvider


class FakeTranslationProvider(TranslationProvider):
    def __init__(self, fail: bool = False, delay_seconds: float = 0.0):
        self.fail = fail
        self.delay_seconds = delay_seconds
        self.usage = {"characters": 0}

    def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.fail:
            raise RuntimeError("fake translation provider configured to fail (translation.fail: true)")
        self.usage["characters"] += len(text)
        return text
