import time

import click
import httpx

from providers.llm.base import LLMProvider

# Shared base for providers that speak the OpenAI chat-completions wire
# format (Mistral, Z.ai) — see docs/adr/021-zai-glm4-flash-provider.md,
# Decision 1. This is a *wire-format* base, not the project's provider
# abstraction: providers/llm/base.py's LLMProvider stays the interface every
# provider implements, and google_aistudio.py (Gemini's native
# generateContent, a genuinely different wire format — ADR 017 Decision 5)
# does not inherit from this at all.
#
# The retry loop below was extracted verbatim from mistral.py, where it was
# hardened against two real production incidents (see the comment block
# below). It once lived in a shared base before, in ADR 015 — reverted along
# with Groq, not because the extraction was wrong. A fix to this loop now
# reaches every OpenAI-wire provider at once; google_aistudio.py keeps its
# own copy and has to be updated separately.
#
# Transient, worth retrying: 429 (rate limit), the 5xx family (server-side,
# usually momentary), AND httpx.TransportError (connection-level failures —
# timeouts, resets, "server disconnected without sending a response" — the
# exception class covering a mid-request network interface change, e.g. a
# LAN dropping and the OS failing over to Wi-Fi). NOT retried: 4xx other than
# 429 (bad request, bad key, etc.) — retrying those just wastes time
# repeating the same mistake.
#
# Found necessary in two separate real incidents on a single long manuscript
# (both on Mistral): a 429 at chunk 51/118 (this project's chunked tasks are
# all-or-nothing — nothing is written until every chunk succeeds, so a
# transient failure deep into a long run is expensive without a retry), and
# later a genuine network interface failover mid-request at chunk 24, which
# raised a connection-level exception, not an HTTP status — the original
# status-code-only retry logic didn't cover that at all, since no response
# was ever received to inspect.
#
# Budget is time-based (not a fixed retry count): keeps retrying, backing off
# up to _MAX_BACKOFF_SECONDS between attempts, until _MAX_RETRY_SECONDS of
# total waiting has elapsed — long enough to ride out a real network hiccup
# (the user asked for "5 minutes standby, or exponential up to 10 minutes"),
# not just a momentary rate limit.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRY_SECONDS = 600  # 10 minutes total, cumulative across all retries
_INITIAL_BACKOFF_SECONDS = 2
_MAX_BACKOFF_SECONDS = 60  # cap per-wait so no single stretch is absurdly long


def _error_detail(response: httpx.Response) -> str:
    """Best-effort extraction of the provider's own error message from a
    failed response body — raise_for_status() alone only reports the status
    code and URL, which was the original complaint behind this: a 429 told
    the user nothing beyond "429".
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


class OpenAIChatProvider(LLMProvider):
    """Subclasses set API_URL/DISPLAY_NAME and override the hooks below for
    whatever genuinely differs per provider. Defaults are no-ops, so a
    subclass that overrides nothing behaves exactly like the original
    mistral.py loop.
    """

    API_URL: str = None
    DISPLAY_NAME: str = None
    # Per-provider, not shared (ADR 021 Decision 4): a provider whose
    # responses can legitimately take longer than this needs its own value,
    # or the client gives up on — and retries — a request that is still
    # being processed server-side.
    REQUEST_TIMEOUT: float = 120.0
    # Appended to the final error message when retries are exhausted on a 429.
    RATE_LIMIT_HINT: str = ""

    def __init__(self, api_key: str, model: str, temperature: float = 0.0,
                 request_pacing_seconds: float = 0.0):
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        # Minimum gap enforced between the *start* of one complete() call and
        # the next on this instance — proactive, unlike everything else in
        # this loop, which only ever reacts to a response already received.
        # 0.0 (the default) is a no-op: MistralProvider doesn't set this, so
        # its behavior is unchanged. Added for ZaiProvider after a real
        # 29-chunk s1b ortho run got three 429s, evenly spread, each cleared
        # by the very next attempt — see docs/adr/021-zai-glm4-flash-provider.md.
        # This does not replace the retry/backoff loop below; it exists to
        # make hitting it less frequent in the first place.
        self._request_pacing_seconds = request_pacing_seconds
        self._last_request_started_at = None
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    # --- hooks --------------------------------------------------------------

    def _extra_payload(self) -> dict:
        """Provider-specific request fields merged into the payload."""
        return {}

    def _error_detail(self, response: httpx.Response) -> str:
        return _error_detail(response)

    def _log_rate_limit_headroom(self, response: httpx.Response) -> None:
        """Called on every successful response — best-effort early warning."""

    def _extra_error_hint(self, response: httpx.Response) -> str:
        """Appended to a non-retryable error's message."""
        return ""

    def _validate_content(self, data: dict) -> str:
        """Extract the reply text from a successful response body."""
        return data["choices"][0]["message"]["content"]

    # --- the shared loop ----------------------------------------------------

    def complete(self, system_prompt: str, user_prompt: str, temperature: float = None) -> str:
        if self._request_pacing_seconds > 0 and self._last_request_started_at is not None:
            wait = self._request_pacing_seconds - (time.monotonic() - self._last_request_started_at)
            if wait > 0.05:
                click.echo(f"    Pacing: waiting {wait:.1f}s before the next "
                           f"request (llm.request_pacing_seconds={self._request_pacing_seconds})...")
                time.sleep(wait)
        self._last_request_started_at = time.monotonic()

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
        name = self.DISPLAY_NAME

        attempt = 0
        elapsed = 0.0
        while True:
            network_exc, response, label = None, None, None
            try:
                response = httpx.post(self.API_URL, headers=headers, json=payload, timeout=self.REQUEST_TIMEOUT)
            except httpx.TransportError as exc:
                network_exc = exc
                label = f"network error ({exc.__class__.__name__}: {exc})"
            else:
                if response.status_code not in _RETRYABLE_STATUS:
                    if response.is_error:
                        raise click.ClickException(
                            f"{name} request failed: {response.status_code} — "
                            f"{self._error_detail(response)}{self._extra_error_hint(response)}"
                        )
                    self._log_rate_limit_headroom(response)
                    break  # success
                label = f"{response.status_code} from {name}: {self._error_detail(response)}"

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
                    f"{name} request failed: {response.status_code} after {attempt + 1} "
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
        return self._validate_content(data)
