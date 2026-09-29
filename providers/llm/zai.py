import click
import httpx

from providers.llm.openai_chat import OpenAIChatProvider

# Z.ai (international platform, api.z.ai) — OpenAI chat-completions wire
# format, so everything transport-level (retry/backoff, network errors,
# usage accumulation) comes from openai_chat.py. See
# docs/adr/021-zai-glm4-flash-provider.md.
#
# Found during implementation, before any code was written: ADR 021's
# original default, glm-4-flash, does not exist on api.z.ai ("1211 Unknown
# Model") — it belongs to Zhipu's China platform (bigmodel.cn), as does the
# "over 8K context -> 1% concurrency" throttle the ADR quoted. The default
# became glm-4.7-flash (listed free on Z.ai's pricing page, confirmed working
# with a real call). See the ADR's Implementation Notes.

# Not hardcoded from a figure, deliberately generous (2.5x Mistral's 120s):
# ADR 021 Decision 4. A fixed client timeout that a slow-but-healthy response
# can outlast makes the client give up on — and re-send — a request the
# server is still processing; this project hit exactly that twice with
# google_aistudio.py's 120s timeout. Re-derive once real response times on
# full-size chunks are observed.
_REQUEST_TIMEOUT_SECONDS = 300.0

# Minimum gap enforced between the start of one chunk's request and the
# next (openai_chat.py's request_pacing_seconds — a no-op for Mistral,
# which doesn't set it). A guess, not derived from a confirmed per-second
# ceiling — the same honesty as max_context_chars' 6000 above. Configurable
# via llm.request_pacing_seconds; 0 disables it entirely.
_DEFAULT_REQUEST_PACING_SECONDS = 1.0

# Z.ai signals overload as a 429 carrying its own business code, not a 503:
# a real "429, code 1305: The service may be temporarily overloaded, please
# try again later" was returned during implementation after only a handful
# of tiny requests in about a minute — transient, and it cleared on the next
# attempt. The free tier's documented shape (third-party sources, not
# confirmed against the account) is roughly one concurrent request, which
# this pipeline already respects by sending chunks one at a time.
#
# Confirmed again on a real 29-chunk s1b ortho run (docs/adr/021-zai-glm4-
# flash-provider.md's second Implementation Notes section): three 429s,
# spread roughly every 8 chunks, each cleared on the very next attempt.
# Recovering every time isn't the same as not causing it — see
# _DEFAULT_REQUEST_PACING_SECONDS below, the proactive complement to this
# reactive retry.
_RATE_LIMIT_HINT = (
    " Z.ai returns 429 both for rate limits and for server overload — the "
    "code in the message tells them apart (1305 = temporarily overloaded, "
    "observed for real and usually transient). The free tier's limits are "
    "per account and per model; check your current figures at "
    "z.ai/manage-apikey/rate-limits rather than trusting any number "
    "recorded in this project, none of which have been confirmed against "
    "that page (see docs/adr/021-zai-glm4-flash-provider.md)."
)

# A finish_reason other than "stop" means the text is unusable as a
# faithful transformation of the chunk — "length" means it was truncated at
# the output-token cap, and anything else (e.g. a content filter) is a
# refusal. Deterministic for the same input, so raised, never retried; the
# ADR 017 Decision 3 principle, applied to this wire format.
_LENGTH_GUIDANCE = (
    "the reply hit the output-token cap and is truncated. Lower max_chars "
    "for this task (or llm.limits.max_context_chars) — retrying will not "
    "help, since the same input produces the same truncation."
)


class ZaiProvider(OpenAIChatProvider):
    API_URL = "https://api.z.ai/api/paas/v4/chat/completions"
    DISPLAY_NAME = "Z.ai"
    REQUEST_TIMEOUT = _REQUEST_TIMEOUT_SECONDS
    RATE_LIMIT_HINT = _RATE_LIMIT_HINT

    def __init__(self, api_key: str, model: str, temperature: float = 0.0, thinking: str = "disabled",
                 request_pacing_seconds: float = _DEFAULT_REQUEST_PACING_SECONDS):
        super().__init__(api_key, model, temperature, request_pacing_seconds=request_pacing_seconds)
        self._thinking = thinking

    def _extra_payload(self) -> dict:
        # GLM 4.5+ models reason by default, returned in a separate
        # reasoning_content field and billed/timed as output. Off by default
        # for the same reason Gemini runs at thinking_level: low (ADR 017
        # Decision 2): this pipeline's tasks are transformation, not
        # problem-solving. Confirmed for real: "disabled" yields
        # reasoning_tokens: 0 and no reasoning_content on glm-4.7-flash.
        return {"thinking": {"type": self._thinking}}

    def _error_detail(self, response: httpx.Response) -> str:
        # Z.ai's error body is {"error": {"code": "1305", "message": ...}} —
        # the code is what distinguishes overload from a real limit, so keep it.
        detail = super()._error_detail(response)
        try:
            code = (response.json().get("error") or {}).get("code")
        except (ValueError, AttributeError):
            code = None
        return f"{detail} (code {code})" if code else detail

    def _validate_content(self, data: dict) -> str:
        choices = data.get("choices") or []
        if not choices:
            raise click.ClickException(
                f"Z.ai returned no choices — unexpected response shape. Raw response "
                f"(truncated): {str(data)[:300]}"
            )
        finish_reason = choices[0].get("finish_reason")
        if finish_reason != "stop":
            guidance = _LENGTH_GUIDANCE if finish_reason == "length" else (
                "the model did not complete normally; see "
                "docs/adr/021-zai-glm4-flash-provider.md."
            )
            raise click.ClickException(
                f"Z.ai did not complete generation: finish_reason={finish_reason!r} — {guidance}"
            )
        text = (choices[0].get("message") or {}).get("content") or ""
        if not text.strip():
            raise click.ClickException(
                "Z.ai returned an empty reply despite finish_reason='stop' (if "
                "llm.thinking is enabled, the output may have gone to "
                "reasoning_content instead). See docs/adr/021-zai-glm4-flash-provider.md."
            )
        return text
