"""Deterministic LLM provider for tests (ADR 023, T0.2). Never contacts a
network, needs no API key, and is selected with `llm.provider: fake`.

Its output satisfies every structural contract code parses downstream, not
any quality bar: an `<overview>` block (read by `metadata_doc`), `<explorer>`
and `<dish>` (read by `newsletter_track`), a `<verdict>` line (read by
`candidate record-decision`), at least one `## ` heading, and an excerpt of
the input so a test can see the right text reached the right call.

Config keys, both optional, read under `llm:`:
- `fail: true` makes every call raise.
- `delay_seconds: N` sleeps N seconds before answering (cancel tests).
"""
import time

from providers.llm.base import LLMProvider

_EXCERPT_CHARS = 200
_CHARS_PER_TOKEN = 4


def _excerpt(text: str) -> str:
    # Angle brackets are neutralised so an excerpt of a previous fake output
    # (briefs feed the expansion tasks) can never open a second structural
    # tag that a downstream regex would then match instead of the real one.
    return text[:_EXCERPT_CHARS].replace("<", "‹").replace(">", "›")


class FakeLLMProvider(LLMProvider):
    def __init__(self, fail: bool = False, delay_seconds: float = 0.0):
        self.fail = fail
        self.delay_seconds = delay_seconds
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def complete(self, system_prompt: str, user_prompt: str, temperature: float = None) -> str:
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.fail:
            raise RuntimeError("fake LLM provider configured to fail (llm.fail: true)")

        excerpt = _excerpt(user_prompt)
        text = (
            "## Resumen de prueba\n\n"
            f"<overview>Descripcion de prueba generada por el proveedor simulado. {excerpt}</overview>\n\n"
            "<explorer>Exploradora de Prueba</explorer>\n"
            "<dish>Plato de Prueba</dish>\n\n"
            "<verdict>Encaja</verdict>\n\n"
            f"{excerpt}"
        )

        prompt_tokens = (len(system_prompt) + len(user_prompt)) // _CHARS_PER_TOKEN
        completion_tokens = len(text) // _CHARS_PER_TOKEN
        self.usage["prompt_tokens"] += prompt_tokens
        self.usage["completion_tokens"] += completion_tokens
        self.usage["total_tokens"] += prompt_tokens + completion_tokens
        return text
