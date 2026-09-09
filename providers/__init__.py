import os

import click

from providers.llm.base import LLMProvider
from providers.translation.base import TranslationProvider


def get_llm_provider(config: dict) -> LLMProvider:
    name = config["llm"]["provider"]
    # No provider-agnostic default model name makes sense (see
    # docs/adr/015-groq-llm-provider.md) — checked once here, before
    # dispatching, rather than duplicated as a per-provider fallback.
    model = config["llm"].get("model")
    if not model:
        raise click.ClickException(
            "llm.model is not set in config.yaml. There is no sensible "
            "default across providers — set it explicitly (e.g. "
            "'mistral-medium-latest' for Mistral, 'openai/gpt-oss-120b' "
            "for Groq)."
        )
    if name == "mistral":
        api_key = os.environ.get("MISTRAL_API_KEY")
        if not api_key:
            raise click.ClickException(
                "MISTRAL_API_KEY is not set. Add it to your .env file."
            )
        from providers.llm.mistral import MistralProvider
        return MistralProvider(
            api_key=api_key,
            model=model,
            temperature=config["llm"].get("temperature", 0.0),
        )
    if name == "groq":
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise click.ClickException(
                "GROQ_API_KEY is not set. Add it to your .env file."
            )
        from providers.llm.groq import GroqProvider
        return GroqProvider(
            api_key=api_key,
            model=model,
            temperature=config["llm"].get("temperature", 0.0),
            reasoning_effort=config["llm"].get("reasoning_effort"),
        )
    raise ValueError(f"Unknown LLM provider: {name!r}. Supported: mistral, groq")


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
