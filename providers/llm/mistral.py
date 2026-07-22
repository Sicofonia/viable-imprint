import time

import click
import httpx

from providers.llm.base import LLMProvider

_API_URL = "https://api.mistral.ai/v1/chat/completions"

# Transient, worth retrying: 429 (rate limit) and the 5xx family (server-side,
# usually momentary). NOT retried: 4xx other than 429 (bad request, bad key,
# etc.) — retrying those just wastes time repeating the same mistake. Found
# necessary on a real ~118-chunk run that died on a single 429 at chunk 51,
# losing every prior chunk's work — this project's chunked tasks are
# all-or-nothing (nothing is written until every chunk succeeds), so a
# transient failure this deep into a long run is expensive without a retry.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5
_BACKOFF_SECONDS = 2  # doubles each retry: 2, 4, 8, 16, 32


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
        while True:
            response = httpx.post(_API_URL, headers=headers, json=payload, timeout=120.0)
            if response.status_code not in _RETRYABLE_STATUS or attempt >= _MAX_RETRIES:
                response.raise_for_status()
                break
            attempt += 1
            # Respect the server's own Retry-After if it sent one; otherwise
            # exponential backoff. Rate-limit responses often include this.
            retry_after = response.headers.get("retry-after")
            wait = float(retry_after) if retry_after else _BACKOFF_SECONDS * (2 ** (attempt - 1))
            click.echo(f"    {response.status_code} from Mistral, retrying in {wait:.0f}s "
                       f"(attempt {attempt}/{_MAX_RETRIES})...")
            time.sleep(wait)

        data = response.json()
        usage = data.get("usage", {})
        for field in self.usage:
            self.usage[field] += usage.get(field, 0)
        return data["choices"][0]["message"]["content"]
