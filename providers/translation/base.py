from abc import ABC, abstractmethod


class TranslationProvider(ABC):
    """Concrete providers are expected (not enforced — see ADR 005) to expose
    a `usage` dict on the instance, accumulated across every `translate()`
    call made on it: {"characters": int}. Same convention as
    `LLMProvider.usage` — one instance per task run, read once after the loop.
    """

    @abstractmethod
    def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        """Translate text. Language codes are ISO 639-1 (e.g. EN, ES)."""
        ...
