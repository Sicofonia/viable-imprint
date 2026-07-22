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
                    response.raise_for_status()
                    break  # success
                label = f"{response.status_code} from Mistral"

            retry_after = response.headers.get("retry-after") if response is not None else None
            wait = float(retry_after) if retry_after else min(
                _INITIAL_BACKOFF_SECONDS * (2 ** attempt), _MAX_BACKOFF_SECONDS
            )

            if elapsed + wait > _MAX_RETRY_SECONDS:
                click.echo(f"    Giving up after {elapsed:.0f}s of retries ({label}).")
                if network_exc is not None:
                    raise network_exc
                response.raise_for_status()

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
