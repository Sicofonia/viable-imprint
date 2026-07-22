import re
from pathlib import Path

import deepl
from providers.translation.base import TranslationProvider


class DeepLProvider(TranslationProvider):
    def __init__(self, api_key: str):
        self._translator = deepl.Translator(api_key)
        self.usage = {"characters": 0}

    def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        """Translate while preserving [i] and [sc] markup.

        DeepL's xml tag_handling keeps tag structure intact across translation.
        We convert our custom square-bracket markup to XML before the call
        and convert back after, so markup survives the translation round-trip.
        """
        xml_text = self._to_xml(text)
        wrapped = f"<doc>{xml_text}</doc>"

        result = self._translator.translate_text(
            wrapped,
            source_lang=source_lang.upper(),
            target_lang=target_lang.upper(),
            tag_handling="xml",
        )
        self.usage["characters"] += result.billed_characters

        translated = result.text
        # Strip the <doc>…</doc> wrapper we added
        if translated.startswith("<doc>") and translated.endswith("</doc>"):
            translated = translated[5:-6]

        return self._from_xml(translated)

    def translate_document(self, input_path: Path, output_path: Path, source_lang: str, target_lang: str) -> None:
        """Translate a whole file server-side via DeepL's document-translation
        endpoint, no chunking. Unlike `translate()`, there is NO tag_handling
        equivalent for this endpoint — `[i]`/`[sc]` bracket markup is sent as
        literal text, not specially preserved. Whether it survives is an open,
        empirically-tested question (see docs/adr for this feature), not
        assumed here. `self.usage` is updated the same way as `translate()`,
        from `DocumentStatus.billed_characters`.
        """
        status = self._translator.translate_document_from_filepath(
            input_path,
            output_path,
            source_lang=source_lang.upper(),
            target_lang=target_lang.upper(),
        )
        if status.billed_characters is not None:
            self.usage["characters"] += status.billed_characters

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_xml(text: str) -> str:
        # Escape bare ampersands that would break XML parsing
        text = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;)", "&amp;", text)
        text = text.replace("[i]", "<em>").replace("[/i]", "</em>")
        text = text.replace("[sc]", "<sc>").replace("[/sc]", "</sc>")
        return text

    @staticmethod
    def _from_xml(text: str) -> str:
        text = text.replace("&amp;", "&")
        text = text.replace("<em>", "[i]").replace("</em>", "[/i]")
        text = text.replace("<sc>", "[sc]").replace("</sc>", "[/sc]")
        return text
