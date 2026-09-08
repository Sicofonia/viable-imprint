import time

import click
import httpx

from providers.llm.base import LLMProvider

_API_URL = "https://api.mistral.ai/v1/chat/completions"

# Transient, worth retrying: 429 (rate limit), the 5xx family (server-side,
# usually momentary), AND httpx.TransportError (connection-level failures —
# timeouts, resets, "server disconnected without sending a response" — the
# exception class covering a mid-request network interface change, e.g. a
# LAN dropping and the OS failing over to Wi-Fi). NOT retried: 4xx other than
# 429 (bad request, bad key, etc.) — retrying those just wastes time
# repeating the same mistake.
#
# Found necessary in two separate real incidents on a single long manuscript:
# a 429 at chunk 51/118 (this project's chunked tasks are all-or-nothing —
# nothing is written until every chunk succeeds, so a transient failure deep
# into a long run is expensive without a retry), and later a genuine network
# interface failover mid-request at chunk 24, which raised a connection-level
# exception, not an HTTP status — the original status-code-only retry logic
# didn't cover that at all, since no response was ever received to inspect.
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


def _error_detail(response: httpx.Response) -> str:
    """Best-effort extraction of Mistral's own error message from a failed
    response body — raise_for_status() alone only reports the status code
    and URL, which was the original complaint behind this: a 429 told the
    user nothing beyond "429".
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


def _log_rate_limit_if_low(response: httpx.Response) -> None:
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


class MistralProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "mistral-medium-latest", temperature: float = 0.0):
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
        }
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

        attempt = 0
        elapsed = 0.0
        while True:
            network_exc, response, label = None, None, None
            try:
                response = httpx.post(_API_URL, headers=headers, json=payload, timeout=120.0)
            except httpx.TransportError as exc:
                network_exc = exc
                label = f"network error ({exc.__class__.__name__}: {exc})"
            else:
                if response.status_code not in _RETRYABLE_STATUS:
                    if response.is_error:
                        raise click.ClickException(
                            f"Mistral request failed: {response.status_code} — {_error_detail(response)}"
                        )
                    _log_rate_limit_if_low(response)
                    break  # success
                label = f"{response.status_code} from Mistral: {_error_detail(response)}"

            retry_after = response.headers.get("retry-after") if response is not None else None
            wait = float(retry_after) if retry_after else min(
                _INITIAL_BACKOFF_SECONDS * (2 ** attempt), _MAX_BACKOFF_SECONDS
            )

            if elapsed + wait > _MAX_RETRY_SECONDS:
                click.echo(f"    Giving up after {elapsed:.0f}s of retries ({label}).")
                if network_exc is not None:
                    raise network_exc
                hint = _RATE_LIMIT_HINT if response.status_code == 429 else ""
                raise click.ClickException(
                    f"Mistral request failed: {response.status_code} after {attempt + 1} "
                    f"attempt(s) over {elapsed:.0f}s — {_error_detail(response)}.{hint}"
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
        return data["choices"][0]["message"]["content"]
