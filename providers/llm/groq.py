"""Groq's chat-completions API (this project runs `openai/gpt-oss-120b`
against it) — the second LLM provider added per docs/adr/015-groq-llm-
provider.md. Groq's endpoint is OpenAI-compatible
(https://api.groq.com/openai/v1/chat/completions), the same wire format
`providers/llm/openai_chat.py` already generalizes from the original Mistral
integration, so this file supplies only what's actually different: the
endpoint, GPT-OSS-specific request fields (`reasoning_effort`/
`include_reasoning`), Groq's own two-dimensional rate-limit headers, and its
fatal-vs-retryable error distinction (see `_LIMIT_REQUESTED_RE` below —
confirmed against a real call, and not what the ADR originally assumed).
"""
import re

import click

from providers.llm.openai_chat import OpenAIChatProvider

# Groq's free tier is bounded on four dimensions at once — requests/minute,
# requests/day, tokens/minute, tokens/day — and the first ceiling reached
# returns 429. Per Groq's own rate-limits documentation the free-plan ranges
# are RPM 10-30, RPD 100-14.4K, TPM 1.2K-15K, TPD 3.6K-500K;
# openai/gpt-oss-120b specifically sits at roughly 30 RPM / 1,000 RPD /
# 8,000 TPM per secondary sources — NOT independently confirmed against this
# account's own dashboard. Verify at console.groq.com/settings/limits before
# trusting these figures for anything more than an order-of-magnitude sense
# of the ceiling. See docs/adr/015-groq-llm-provider.md, "The constraint
# that is not a drop-in".
_RATE_LIMIT_HINT = (
    " Groq's free tier is bounded on requests/minute, requests/day, "
    "tokens/minute, AND tokens/day — the first ceiling hit returns 429, "
    "regardless of the others' headroom. openai/gpt-oss-120b's free-tier "
    "figures are roughly 30 RPM / 1,000 RPD / 8,000 TPM (unverified against "
    "this account — confirm at console.groq.com/settings/limits). See "
    "docs/adr/015-groq-llm-provider.md."
)

# CORRECTED against a real call (see docs/adr/015-groq-llm-provider.md's
# Implementation notes) — the ADR's original assumption was that "this
# single request alone exceeds the ceiling" would surface as a 429; it
# doesn't. A real oversized request (largest single_chunk task, preflight
# disabled) came back as **HTTP 413**, not 429: "Request too large for model
# `openai/gpt-oss-120b`... on tokens per minute (TPM): Limit 8000, Requested
# 21151, please reduce your message size and try again.". 413 isn't in
# _RETRYABLE_STATUS, so the base class's normal non-retryable-error handling
# already fails fast on it, carrying Groq's own clear message — no special
# handling needed for that path (see _extra_error_hint below for the one
# small addition made because of it).
#
# 429 (`error.code == "rate_limit_exceeded"`) is, per Groq's docs and
# real-world reports, reserved for the genuinely transient case — too many
# requests/tokens used *recently*, where each individual request would be
# fine once the window resets, e.g. "...on requests per minute (RPM): Limit
# 30, Used 30, Requested 1. Please try again in 1.969s...". That's exactly
# the "always retry" case the base class's default already handles.
#
# This regex and _is_fatal_rate_limit() below are kept as a defensive
# fallback, matching both the "Limit N, Used N, Requested N" 429 shape and
# 413's "Limit N, Requested N" (no "Used") shape, in case a 429 with fatal
# arithmetic ever does occur (e.g. a future API change, or a limit dimension
# this pass didn't happen to trigger) — not because it was the path that
# actually fired during real testing. If Groq changes its wording, this
# simply stops matching and falls back to "always retry", the same safe
# default every other provider already has, not a crash.
_LIMIT_REQUESTED_RE = re.compile(r"Limit (\d+),\s*(?:Used \d+,\s*)?Requested (\d+)")


class GroqProvider(OpenAIChatProvider):
    API_URL = "https://api.groq.com/openai/v1/chat/completions"
    DISPLAY_NAME = "Groq"
    RATE_LIMIT_HINT = _RATE_LIMIT_HINT

    def __init__(self, api_key: str, model: str, temperature: float = 0.0, reasoning_effort: str = None):
        super().__init__(api_key, model, temperature)
        self._reasoning_effort = reasoning_effort

    def _extra_payload(self) -> dict:
        # include_reasoning: false unconditionally — belt and braces, since
        # GPT-OSS models are documented to place reasoning in a separate
        # `reasoning` field rather than `message.content`; this pipeline
        # only ever wants the final answer. reasoning_effort is included
        # only when configured, letting Groq's own default ("medium") apply
        # otherwise rather than this project silently picking one.
        payload = {"include_reasoning": False}
        if self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort
        return payload

    def _log_rate_limit_headroom(self, response) -> None:
        # Groq's headers are two-dimensional (requests AND tokens, each with
        # its own limit/remaining/reset) with different names from Mistral's
        # single x-ratelimit-remaining, and duration-string reset values
        # ("2m59.56s") rather than numeric seconds — echoed as-is, not
        # parsed. Only the tokens-per-minute dimension is checked here: it's
        # the one this pipeline's chunked tasks actually run up against (see
        # the ADR's "constraint that is not a drop-in" section); the
        # requests-per-day dimension is far less likely to bind first.
        remaining = response.headers.get("x-ratelimit-remaining-tokens")
        limit = response.headers.get("x-ratelimit-limit-tokens")
        if remaining is None or limit is None:
            return
        try:
            remaining_n = float(remaining)
            limit_n = float(limit)
        except ValueError:
            return
        if limit_n and remaining_n <= limit_n * 0.2:
            reset = response.headers.get("x-ratelimit-reset-tokens")
            resets_in = f", resets in {reset}" if reset else ""
            click.echo(
                f"    Rate limit headroom low: {remaining} of {limit} "
                f"tokens/minute remaining{resets_in}."
            )

    def _is_fatal_rate_limit(self, response) -> bool:
        """True when the response's own "Limit N ... Requested N" arithmetic
        shows this single request's Requested figure alone exceeds the
        Limit — inadmissible regardless of any Used figure also present,
        since Used resets over time but Requested doesn't shrink. Defensive
        fallback for a 429 shaped this way; see the module-level comment on
        _LIMIT_REQUESTED_RE for why the case this was originally written for
        (a structurally oversized single request) turned out to surface as
        413 instead, which never reaches this hook at all.
        """
        match = _LIMIT_REQUESTED_RE.search(self._error_detail(response))
        if not match:
            return False
        limit, requested = (int(g) for g in match.groups())
        return requested > limit

    def _extra_error_hint(self, response) -> str:
        # 413 is what a structurally oversized single request actually gets
        # back (see _LIMIT_REQUESTED_RE above) — confirmed against a real
        # call. Point at the mechanism that's supposed to catch this before
        # it ever reaches Groq, in case it's disabled or set too high.
        if response.status_code == 413:
            return (
                " This pipeline's own llm.max_request_tokens preflight "
                "(config.yaml) is meant to catch this before spending a "
                "request on it — check it's set, or lower max_chars for "
                "this task. See docs/adr/015-groq-llm-provider.md."
            )
        return ""

    def _validate_content(self, content: str) -> None:
        if not content or not content.strip():
            raise click.ClickException(
                "Groq returned an empty response body. gpt-oss models can "
                "place their output in a separate `reasoning` field instead "
                "of `message.content` under some configurations — check "
                "this account's reasoning_effort/include_reasoning "
                "behavior. See docs/adr/015-groq-llm-provider.md."
            )
