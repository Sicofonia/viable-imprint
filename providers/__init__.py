import os

import click

from providers.llm.base import LLMProvider
from providers.translation.base import TranslationProvider


def get_llm_provider(config: dict) -> LLMProvider:
    name = config["llm"]["provider"]
    if name == "mistral":
        api_key = os.environ.get("MISTRAL_API_KEY")
        if not api_key:
            raise click.ClickException(
                "MISTRAL_API_KEY is not set. Add it to your .env file."
            )
        from providers.llm.mistral import MistralProvider
        return MistralProvider(
            api_key=api_key,
            model=config["llm"].get("model", "mistral-medium-latest"),
            temperature=config["llm"].get("temperature", 0.0),
        )
    raise ValueError(f"Unknown LLM provider: {name!r}. Supported: mistral")


def get_translation_provider(config: dict) -> TranslationProvider:
    name = config["translation"]["provider"]
    if name == "deepl":
        api_key = os.environ.get("DEEPL_API_KEY")
        if not api_key:
            raise click.ClickException(
                "DEEPL_API_KEY is not set. Add it to your .env file."
            )
        from providers.translation.deepl import DeepLProvider
        return DeepLProvider(api_key=api_key)
    raise ValueError(f"Unknown translation provider: {name!r}. Supported: deepl")
