"""Mistral's chat-completions API — the original LLM provider for this
project. Its retry/backoff/diagnostics machinery now lives in
`providers/llm/openai_chat.py`, shared with every provider that speaks the
same OpenAI-compatible wire format (Groq, as of docs/adr/015-groq-llm-
provider.md) — this file supplies only what's actually Mistral-specific:
the endpoint, the account-tier rate-limit diagnosis, and the
`X-RateLimit-Remaining` headroom echo.
"""
import click

from providers.llm.openai_chat import OpenAIChatProvider

# A 429 here is overwhelmingly a rate-limit-tier issue, not the account's
# monthly usage quota — confirmed against Mistral's own help center (search
# "Why am I hitting API rate limits?"): a fresh account sits in Free mode's
# minimal per-second/per-minute caps regardless of billing, and *adding
# credits does not raise that tier* — only cumulative billed spend does
# (pay-as-you-go enabled -> Tier 1, past $20 billed -> Tier 2, etc). That's
# why a 429 can happen with 0% of the month's usage consumed: usage % and
# rate-limit tier are unrelated numbers. Originally this hint pointed at the
# X-RateLimit-* response headers instead, per docs.mistral.ai/resources/
# known-limitations — dropped after a real run showed no such header on an
# actual 429 body, and a live GitHub issue (pydantic/pydantic-ai#1885)
# corroborates Mistral not reliably sending it despite the docs.
_RATE_LIMIT_HINT = (
    " Mistral ties rate limits to account tier, not monthly usage: a fresh "
    "account starts on Free mode's minimal per-second/per-minute caps, and "
    "only cumulative billed spend — not adding credits — raises that tier. "
    "See help.mistral.ai, 'Why am I hitting API rate limits, and how do I "
    "increase them?'."
)

# Threshold for the low-headroom echo below: fraction of the limit left in
# the current window. Mistral's docs tip is to watch X-RateLimit-Remaining
# to catch a rate limit coming before it produces a 429 — kept as a
# best-effort check (see _RATE_LIMIT_HINT above for why it's not relied on
# for the main diagnosis): harmless to check, but real-world reports suggest
# Mistral doesn't reliably send this header, so don't count on it firing.
_LOW_RATE_LIMIT_RATIO = 0.2


class MistralProvider(OpenAIChatProvider):
    API_URL = "https://api.mistral.ai/v1/chat/completions"
    DISPLAY_NAME = "Mistral"
    RATE_LIMIT_HINT = _RATE_LIMIT_HINT

    def _log_rate_limit_headroom(self, response) -> None:
        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining is None:
            return
        limit = response.headers.get("x-ratelimit-limit")
        try:
            remaining_n = float(remaining)
            limit_n = float(limit) if limit is not None else None
        except ValueError:
            return
        low = remaining_n <= limit_n * _LOW_RATE_LIMIT_RATIO if limit_n else remaining_n <= 5
        if low:
            reset = response.headers.get("x-ratelimit-reset")
            of_limit = f" of {limit}" if limit is not None else ""
            resets_in = f", resets in {reset}s" if reset is not None else ""
            click.echo(f"    Rate limit headroom low: {remaining}{of_limit} remaining{resets_in}.")
