import httpx
from providers.llm.base import LLMProvider

_API_URL = "https://api.mistral.ai/v1/chat/completions"


class MistralProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "mistral-medium-latest", temperature: float = 0.0):
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def complete(self, system_prompt: str, user_prompt: str, temperature: float = None) -> str:
        response = httpx.post(
            _API_URL,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "temperature": temperature if temperature is not None else self._temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=120.0,
        )
        response.raise_for_status()
        data = response.json()
        usage = data.get("usage", {})
        for field in self.usage:
            self.usage[field] += usage.get(field, 0)
        return data["choices"][0]["message"]["content"]
