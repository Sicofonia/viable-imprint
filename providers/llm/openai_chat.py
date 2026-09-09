"""Shared base for LLM providers that speak the OpenAI chat-completions wire
format: POST a system+user message pair to a `/chat/completions`-shaped
endpoint, read `choices[0].message.content` and a `usage` object back.
Mistral and Groq both do — Groq deliberately mirrors OpenAI's API shape, and
Mistral's own API happens to match it closely enough that this project's
original httpx-based Mistral integration (never an SDK — see
docs/adr/015-groq-llm-provider.md) needed no real changes to become the base
both providers share.

This is a wire-format base, not this project's LLM abstraction — that stays
`providers/llm/base.py`'s two-method `LLMProvider` interface. A future
provider whose wire format differs (e.g. Anthropic's Messages API)
implements `LLMProvider` directly, without inheriting anything from here.

Concrete providers set the class attributes and override the hook methods
below; everything else — payload construction, the retry/backoff loop,
usage accumulation — is shared, and was moved here unchanged from
`mistral.py`, this project's original (and, until now, only) LLM provider.
See docs/adr/015-groq-llm-provider.md, Decision 1.
"""
import time

import click
import httpx

from providers.llm.base import LLMProvider

# Transient, worth retrying: 429 (rate limit), the 5xx family (server-side,
# usually momentary), AND httpx.TransportError (connection-level failures —
# timeouts, resets, "server disconnected without sending a response" — the
# exception class covering a mid-request network interface change, e.g. a
# LAN dropping and the OS failing over to Wi-Fi). NOT retried: 4xx other than
# 429 (bad request, bad key, etc.) — retrying those just wastes time
# repeating the same mistake.
#
# Found necessary in two separate real incidents on a single long manuscript,
# against the original (Mistral-only) provider: a 429 at chunk 51/118 (this
# project's chunked tasks are all-or-nothing — nothing is written until every
# chunk succeeds, so a transient failure deep into a long run is expensive
# without a retry), and later a genuine network interface failover
# mid-request at chunk 24, which raised a connection-level exception, not an
# HTTP status — the original status-code-only retry logic didn't cover that
# at all, since no response was ever received to inspect.
#
# Budget is time-based (not a fixed retry count): keeps retrying, backing off
# up to _MAX_BACKOFF_SECONDS between attempts, until _MAX_RETRY_SECONDS of
# total waiting has elapsed — long enough to ride out a real network hiccup
# ("5 minutes standby, or exponential up to 10 minutes"), not just a
# momentary rate limit.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRY_SECONDS = 600  # 10 minutes total, cumulative across all retries
_INITIAL_BACKOFF_SECONDS = 2
_MAX_BACKOFF_SECONDS = 60  # cap per-wait so no single stretch is absurdly long


class OpenAIChatProvider(LLMProvider):
    """Concrete subclasses set `API_URL` and `DISPLAY_NAME` (required), and
    optionally `RATE_LIMIT_HINT` plus whichever hooks their provider needs —
    see the table in docs/adr/015-groq-llm-provider.md, Decision 1. Every
    hook has a safe no-op default, so a minimal subclass needs only the two
    required class attributes.
    """

    API_URL: str = None
    DISPLAY_NAME: str = None
    # Appended to the final failure message once retries are exhausted on a
    # 429 — provider-specific context on *why* (tier vs. usage, free-tier
    # dimensions, etc.). Empty by default.
    RATE_LIMIT_HINT: str = ""

    def __init__(self, api_key: str, model: str, temperature: float = 0.0):
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def complete(self, system_prompt: str, user_prompt: str, temperature: float = None) -> str:
        payload = {
            "model": self._model,
            "temperature": temperature if temperature is not None else self._temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            **self._extra_payload(),
        }
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

        attempt = 0
        elapsed = 0.0
        while True:
            network_exc, response, label = None, None, None
            try:
                response = httpx.post(self.API_URL, headers=headers, json=payload, timeout=120.0)
            except httpx.TransportError as exc:
                network_exc = exc
                label = f"network error ({exc.__class__.__name__}: {exc})"
            else:
                if response.status_code not in _RETRYABLE_STATUS:
                    if response.is_error:
                        raise click.ClickException(
                            f"{self.DISPLAY_NAME} request failed: {response.status_code} — "
                            f"{self._error_detail(response)}{self._extra_error_hint(response)}"
                        )
                    self._log_rate_limit_headroom(response)
                    break  # success
                if response.status_code == 429 and self._is_fatal_rate_limit(response):
                    # Not transient — the response itself says this request
                    # exceeds a hard ceiling (e.g. a single call over the
                    # whole per-minute token budget). No amount of waiting
                    # admits it, so fail now instead of spending the retry
                    # budget re-sending an inadmissible request. See
                    # docs/adr/015-groq-llm-provider.md, Decision 4.
                    raise click.ClickException(
                        f"{self.DISPLAY_NAME} rejected this request outright (not a "
                        f"transient rate limit — no retry attempted): {response.status_code} "
                        f"— {self._error_detail(response)}"
                    )
                label = f"{response.status_code} from {self.DISPLAY_NAME}: {self._error_detail(response)}"

            retry_after = response.headers.get("retry-after") if response is not None else None
            wait = float(retry_after) if retry_after else min(
                _INITIAL_BACKOFF_SECONDS * (2 ** attempt), _MAX_BACKOFF_SECONDS
            )

            if elapsed + wait > _MAX_RETRY_SECONDS:
                click.echo(f"    Giving up after {elapsed:.0f}s of retries ({label}).")
                if network_exc is not None:
                    raise network_exc
                hint = self.RATE_LIMIT_HINT if response.status_code == 429 else ""
                raise click.ClickException(
                    f"{self.DISPLAY_NAME} request failed: {response.status_code} after {attempt + 1} "
                    f"attempt(s) over {elapsed:.0f}s — {self._error_detail(response)}.{hint}"
                )

            attempt += 1
            elapsed += wait
            click.echo(f"    {label}, retrying in {wait:.0f}s "
                       f"(elapsed {elapsed:.0f}s / {_MAX_RETRY_SECONDS}s budget)...")
            time.sleep(wait)

        data = response.json()
        usage = data.get("usage", {})
        for field in self.usage:
            self.usage[field] += usage.get(field, 0)
        content = data["choices"][0]["message"]["content"]
        self._validate_content(content)
        return content

    # ------------------------------------------------------------------
    # Hooks — override in a concrete provider as needed. See the table in
    # docs/adr/015-groq-llm-provider.md, Decision 1.
    # ------------------------------------------------------------------

    def _extra_payload(self) -> dict:
        """Extra fields merged into the request payload. Default: none."""
        return {}

    def _log_rate_limit_headroom(self, response: httpx.Response) -> None:
        """Called once, on a successful (non-retried) response, to echo a
        low-headroom warning if the provider's rate-limit headers say so.
        Default: no-op — header names and shapes differ per provider, so
        there's nothing safe to check generically.
        """

    def _is_fatal_rate_limit(self, response: httpx.Response) -> bool:
        """Called on a 429 before it's queued for retry. Return True when
        the response itself says the request could never succeed (e.g. its
        own token count exceeds the account's per-minute ceiling), so the
        caller fails immediately instead of burning the retry budget on a
        request that was never admissible. Default: False (always retry a
        429), preserving this project's original Mistral behavior verbatim.
        """
        return False

    def _validate_content(self, content: str) -> None:
        """Called on a successful response's extracted content, before it's
        returned. Override to raise if a provider can return an empty or
        otherwise unusable body under some configuration. Default: no check,
        preserving this project's original Mistral behavior verbatim.
        """

    def _extra_error_hint(self, response: httpx.Response) -> str:
        """Optional text appended to a non-retryable error's message (the
        branch that fires for any error status outside _RETRYABLE_STATUS,
        e.g. 400/401/413). Default: none.
        """
        return ""

    # ------------------------------------------------------------------
    # Shared helper
    # ------------------------------------------------------------------

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        """Best-effort extraction of a provider's own error message from a
        failed response body — raise_for_status() alone only reports the
        status code and URL, which was the original complaint behind this: a
        429 told the user nothing beyond "429". Both Mistral's and Groq's
        error bodies use the same `{"message"/"detail"/"error": ...}` shape.
        """
        try:
            body = response.json()
        except ValueError:
            text = response.text.strip()
            return text[:300] if text else "(no response body)"
        message = body.get("message") or body.get("detail") or body.get("error")
        if isinstance(message, dict):
            message = message.get("message", message)
        return str(message) if message else response.text.strip()[:300]
