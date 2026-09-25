import time

import click
import httpx

from providers.llm.base import LLMProvider

# Google AI Studio's Gemini Developer API. Native generateContent, not the
# OpenAI-compatibility endpoint or the newer Interactions API — see
# docs/adr/017-google-aistudio-llm-provider.md, Decision 6, for why, and the
# named triggers for revisiting that.
_API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Deliberately self-contained rather than sharing providers/llm/mistral.py's
# retry/backoff/diagnostics loop, on explicit instruction — see ADR 017,
# Decision 5. mistral.py is NOT touched by this file's existence. This is a
# real, named tradeoff: two copies of the subtlest code in this repository
# (it absorbed two separate real production incidents' worth of hardening,
# see mistral.py's own comments) means the next fix to it has to be applied
# twice or silently misses one. A third LLM provider is the trigger to
# extract this into a shared plain function (url/headers/payload/
# display-name) — not a class hierarchy; Gemini's wire format being
# genuinely incompatible with Mistral's is exactly why a shared *chat-format*
# base (what the reverted ADR 015 built for Mistral+Groq) was the wrong seam
# here. The retry loop itself is HTTP-transport-level and provider-agnostic;
# only the two constants below and the two Gemini-specific hooks
# (_error_detail, _RATE_LIMIT_HINT) actually differ from mistral.py's copy.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRY_SECONDS = 600  # 10 minutes total, cumulative across all retries
_INITIAL_BACKOFF_SECONDS = 2
_MAX_BACKOFF_SECONDS = 60  # cap per-wait so no single stretch is absurdly long

# ADR 020: how long one model may return 503 continuously before the loop
# switches to the next llm.fallback_models entry (opt-in; no effect unless
# fallback_models is configured). Only 503 counts toward it — never a
# timeout, a 429, or a 200-level finishReason failure.
_DEFAULT_FALLBACK_AFTER_SECONDS = 60.0

# Google no longer publishes a rate-limit table (its docs point at each
# account's own AI Studio dashboard instead). Free-tier quotas ARE tracked
# PER MODEL — confirmed the hard way during implementation, see ADR 017's
# original Implementation Notes (2026-09-10) — but the assumed MAGNITUDE
# for every model other than gemini-3.8-flash was wrong, corrected via
# further real account testing on 2026-09-11 (see ADR 017's Context
# "Correction (2026-09-11)" and its second, 2026-09-11-dated Implementation
# Notes section): every model in the line — 3.8, 3.7, 3.6, 3.5 — shares the
# SAME flat free-tier ceiling, roughly 5 requests/minute, 20 requests/day
# (peak), 250,000 input tokens/minute (peak). The per-model BUCKET
# mechanism is real (a 3.8 429 does not affect 3.6's own quota, confirmed
# directly); what was wrong was believing 3.6/3.7/3.5 had a much larger
# ~1,500 RPD bucket than 3.8's 20 — they do not. At 20 RPD flat, request
# COUNT is the binding constraint for this pipeline, not tokens — a 429
# here is expected to recur, routinely, well before any TPM/RPM ceiling is
# ever approached, on anything beyond a small single-task run.
_RATE_LIMIT_HINT = (
    " Google AI Studio's real, confirmed free tier for this model line is "
    "roughly 5 requests/minute, 20 requests/day, and 250,000 input tokens/"
    "minute (all peak figures) — confirm your account's current limits at "
    "aistudio.google.com/rate-limit. A 429 here is RESOURCE_EXHAUSTED; at "
    "only 20 requests/day, this is expected to fire routinely on anything "
    "beyond a small single-task run, not a sign of a problem. Each model "
    "in this line (3.8/3.7/3.6/3.5) is tracked in its own separate quota "
    "bucket, but at this SAME flat 20 RPD ceiling — switching to a "
    "different Flash release will NOT help, since it shares the same "
    "daily cap, not a larger one. If it persists for many minutes on a "
    "single, unhurried request with nothing else running, the day's "
    "20-request budget for this model is most likely already exhausted — "
    "wait for the daily reset rather than retrying, or reduce this task's "
    "request count for future runs (see docs/adr/017-google-aistudio-llm-"
    "provider.md)."
)

# Decision 3's per-finishReason guidance (ADR 017): a blocked or truncated
# generateContent response arrives as a *successful* HTTP 200 carrying
# unusable content, so it never enters the retry loop above — retrying
# would just reach the identical refusal, since e.g. RECITATION is
# deterministic for the same input and model. Each entry is the second
# half of "Google AI Studio did not complete generation: finishReason=X —
# ...", so it reads as one sentence naming what happened and what to do
# about it, not a generic failure.
_RECITATION_GUIDANCE = (
    "the model refused because the output too closely reproduces its "
    "training data. Expected to recur on every retry with this model, "
    "because faithfully reproducing public-domain text is what this task "
    "is for. Run this task against a different provider (e.g. "
    "`pipeline.py --config <other-config>.yaml ...` — Mistral remains "
    "configured), or handle this passage by hand."
)
_CONTENT_FILTER_GUIDANCE = (
    "content filtering blocked the response. Gemini 3 models default "
    "safety filtering to OFF, so this firing at all is worth reading "
    "closely — see docs/adr/017-google-aistudio-llm-provider.md for the "
    "explicit safetySettings remedy, if this recurs."
)
_MAX_TOKENS_GUIDANCE = (
    "output hit the token cap and the response is truncated. Lower "
    "max_chars for this task, or raise the output token limit — retrying "
    "will not help, since the same input produces the same truncation."
)
_FINISH_REASON_GUIDANCE = {
    "RECITATION": _RECITATION_GUIDANCE,
    "SAFETY": _CONTENT_FILTER_GUIDANCE,
    "PROHIBITED_CONTENT": _CONTENT_FILTER_GUIDANCE,
    "BLOCKLIST": _CONTENT_FILTER_GUIDANCE,
    "SPII": _CONTENT_FILTER_GUIDANCE,
    "MAX_TOKENS": _MAX_TOKENS_GUIDANCE,
}
_DEFAULT_FINISH_REASON_GUIDANCE = (
    "see docs/adr/017-google-aistudio-llm-provider.md, Decision 3, for "
    "what this means and how to respond."
)


def _error_detail(response: httpx.Response) -> str:
    """Best-effort extraction of Google's own error message from a failed
    response body — raise_for_status() alone only reports the status code
    and URL. Google's error body is {"error": {"code", "message", "status"}}
    (e.g. "status": "RESOURCE_EXHAUSTED" for a 429) — a different shape
    from Mistral's, but the same underlying problem this function exists
    to fix: a bare status code tells the user nothing.
    """
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:300] if text else "(no response body)"
    error = body.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        status = error.get("status")
        if message:
            return f"{message} ({status})" if status else str(message)
    return response.text.strip()[:300] if response.text.strip() else "(no response body)"


class GoogleAIStudioProvider(LLMProvider):
    def __init__(self, api_key: str, model: str, temperature: float = 0.0, thinking_level: str = "low",
                 fallback_models: list = None, fallback_after_seconds: float = _DEFAULT_FALLBACK_AFTER_SECONDS):
        self._api_key = api_key
        self._temperature = temperature
        self._thinking_level = thinking_level
        # ADR 020: ordered candidates tried after `model` on sustained 503.
        # Sticky for this instance's lifetime (one task run): once switched,
        # later complete() calls stay on the fallback rather than re-paying
        # a saturated primary's window on every chunk.
        self._models = [model] + [m for m in dict.fromkeys(fallback_models or []) if m != model]
        self._model_index = 0
        self._fallback_after_seconds = fallback_after_seconds
        # Models that actually served a request, in first-served order —
        # read by engines/llm_text.py so the ledger records what really ran.
        self.models_used = []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    @property
    def model(self) -> str:
        """The model currently in use (the last one switched to, if any)."""
        return self._models[self._model_index]

    def complete(self, system_prompt: str, user_prompt: str, temperature: float = None) -> str:
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": temperature if temperature is not None else self._temperature,
                "thinkingConfig": {"thinkingLevel": self._thinking_level},
            },
        }
        # The API key goes in a header, not the docs' own `?key=` query
        # parameter — see ADR 017, Decision 1: httpx puts the request URL in
        # its exception messages, and this project records provider error
        # strings into manifest.yaml (lib/task_loader.py's _run_recorded()),
        # so a key in the query string would leak into this project's own
        # error output and into a book's manifest on any transport failure.
        headers = {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}

        attempt = 0
        elapsed = 0.0
        # ADR 020: elapsed-time mark of the current model's first 503 in an
        # unbroken run of them, or None; any other outcome clears it.
        unavailable_since = None
        models_tried = [self.model]
        while True:
            network_exc, response, label = None, None, None
            url = _API_URL_TEMPLATE.format(model=self.model)
            try:
                response = httpx.post(url, headers=headers, json=payload, timeout=120.0)
            except httpx.TransportError as exc:
                network_exc = exc
                label = f"network error ({exc.__class__.__name__}: {exc})"
            else:
                if response.status_code not in _RETRYABLE_STATUS:
                    if response.is_error:
                        raise click.ClickException(
                            f"Google AI Studio request failed: {response.status_code} — "
                            f"{_error_detail(response)}"
                        )
                    break  # success at the HTTP level — see below for generateContent-level checks
                label = f"{response.status_code} from Google AI Studio: {_error_detail(response)}"

            if response is not None and response.status_code == 503:
                if unavailable_since is None:
                    unavailable_since = elapsed
                if (elapsed - unavailable_since >= self._fallback_after_seconds
                        and self._model_index + 1 < len(self._models)):
                    previous = self.model
                    self._model_index += 1
                    models_tried.append(self.model)
                    click.echo(f"    503 on {previous} for {elapsed - unavailable_since:.0f}s — "
                               f"switching to fallback {self.model}.")
                    attempt, unavailable_since = 0, None
                    continue  # straight to the fallback: no wait, same shared budget
            else:
                unavailable_since = None

            retry_after = response.headers.get("retry-after") if response is not None else None
            wait = float(retry_after) if retry_after else min(
                _INITIAL_BACKOFF_SECONDS * (2 ** attempt), _MAX_BACKOFF_SECONDS
            )

            if elapsed + wait > _MAX_RETRY_SECONDS:
                click.echo(f"    Giving up after {elapsed:.0f}s of retries ({label}).")
                if network_exc is not None:
                    raise network_exc
                hint = _RATE_LIMIT_HINT if response.status_code == 429 else ""
                tried = f" Models tried: {', '.join(models_tried)}." if len(models_tried) > 1 else ""
                raise click.ClickException(
                    f"Google AI Studio request failed: {response.status_code} after {attempt + 1} "
                    f"attempt(s) over {elapsed:.0f}s — {_error_detail(response)}.{hint}{tried}"
                )

            attempt += 1
            elapsed += wait
            click.echo(f"    {label}, retrying in {wait:.0f}s "
                       f"(elapsed {elapsed:.0f}s / {_MAX_RETRY_SECONDS}s budget)...")
            time.sleep(wait)

        data = response.json()

        if self.model not in self.models_used:
            self.models_used.append(self.model)

        # Usage is recorded before any of the generateContent-level checks
        # below can raise — a blocked or truncated response still consumed
        # real tokens and should be accounted for, not just a successful one.
        usage = data.get("usageMetadata", {})
        self.usage["prompt_tokens"] += usage.get("promptTokenCount", 0)
        # Thinking tokens are billed as output tokens and cannot be turned
        # off (ADR 017, Context) — omitting thoughtsTokenCount here would
        # silently under-report the real cost of every single call.
        self.usage["completion_tokens"] += (
            usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
        )
        self.usage["total_tokens"] += usage.get("totalTokenCount", 0)

        # See ADR 017, Decision 3: a blocked or truncated generateContent
        # response is a *successful* HTTP call (200, already past the retry
        # loop above) carrying no usable text. Three distinct cases, each
        # raised with a message naming what happened and what to do next —
        # not a generic "generation failed".
        candidates = data.get("candidates") or []
        if not candidates:
            block_reason = (data.get("promptFeedback") or {}).get("blockReason")
            if block_reason:
                raise click.ClickException(
                    f"Google AI Studio blocked the prompt itself: {block_reason}. "
                    f"See docs/adr/017-google-aistudio-llm-provider.md, Decision 3."
                )
            raise click.ClickException(
                "Google AI Studio returned no candidates and no "
                "promptFeedback.blockReason — unexpected response shape. "
                f"Raw response (truncated): {str(data)[:300]}"
            )

        finish_reason = candidates[0].get("finishReason")
        if finish_reason != "STOP":
            guidance = _FINISH_REASON_GUIDANCE.get(finish_reason, _DEFAULT_FINISH_REASON_GUIDANCE)
            raise click.ClickException(
                f"Google AI Studio did not complete generation: "
                f"finishReason={finish_reason!r} — {guidance}"
            )

        # Concatenate every part's text, skipping any part marked
        # thought: true — belt-and-braces, since thought parts are only
        # returned when includeThoughts is set (this provider never sets
        # it) — rather than reading parts[0].text alone, which would
        # silently truncate a response split across multiple parts.
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))

        if not text.strip():
            raise click.ClickException(
                "Google AI Studio returned an empty response body despite "
                "finishReason=STOP. See docs/adr/017-google-aistudio-llm-provider.md."
            )

        return text
