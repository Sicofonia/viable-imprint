from abc import ABC, abstractmethod


class TranslationProvider(ABC):
    """Concrete providers are expected (not enforced — see ADR 005) to expose
    a `usage` dict on the instance, accumulated across every `translate()`
    call made on it: {"characters": int}. Same convention as
    `LLMProvider.usage` — one instance per task run, read once after the loop.

    `translate_document()` is an OPTIONAL capability, same convention as
    `usage` — not an `@abstractmethod`. A provider that doesn't implement it
    simply doesn't support `s1b translate --document`; the engine checks with
    `getattr()` before calling it and fails with a clear message otherwise.
    It exists for providers whose API can translate a whole file server-side
    without this project's own paragraph-chunking (e.g. DeepL's separate
    document-translation endpoint) — a different call shape (file in, file
    out) from `translate()`'s (text in, text out), not a variant of it.
    """

    @abstractmethod
    def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        """Translate text. Language codes are ISO 639-1 (e.g. EN, ES)."""
        ...
