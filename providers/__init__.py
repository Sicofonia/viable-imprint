import os

import click

from providers.llm.base import LLMProvider
from providers.translation.base import TranslationProvider


def get_llm_provider(config: dict) -> LLMProvider:
    name = config["llm"]["provider"]
    # No provider-agnostic default model name makes sense (see
    # docs/adr/017-google-aistudio-llm-provider.md, Decision 7) — a Mistral
    # model name sent to Google AI Studio, or vice versa, simply fails.
    # Checked once here, before dispatching, rather than duplicated as a
    # per-provider fallback.
    model = config["llm"].get("model")
    if not model:
        raise click.ClickException(
            "llm.model is not set in config.yaml. There is no sensible "
            "default across providers — set it explicitly (e.g. "
            "'mistral-medium-latest' for Mistral, 'gemini-3.6-flash' for "
            "Google AI Studio)."
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
    if name == "google-aistudio":
        api_key = os.environ.get("GOOGLE_AISTUDIO_API_KEY")
        if not api_key:
            raise click.ClickException(
                "GOOGLE_AISTUDIO_API_KEY is not set. Add it to your .env file."
            )
        from providers.llm.google_aistudio import GoogleAIStudioProvider, _DEFAULT_FALLBACK_AFTER_SECONDS
        # ADR 020: optional, Google AI Studio only (Mistral ignores both keys).
        fallback_models = config["llm"].get("fallback_models") or []
        if not isinstance(fallback_models, list) or not all(isinstance(m, str) and m for m in fallback_models):
            raise click.ClickException(
                "llm.fallback_models must be a list of model-name strings "
                "(e.g. ['gemini-3.7-flash']) — see config.example.yaml."
            )
        fallback_after = config["llm"].get("fallback_after_seconds", _DEFAULT_FALLBACK_AFTER_SECONDS)
        if isinstance(fallback_after, bool) or not isinstance(fallback_after, (int, float)) or fallback_after < 0:
            raise click.ClickException("llm.fallback_after_seconds must be a non-negative number of seconds.")
        return GoogleAIStudioProvider(
            api_key=api_key,
            model=model,
            temperature=config["llm"].get("temperature", 0.0),
            thinking_level=config["llm"].get("thinking_level", "low"),
            fallback_models=fallback_models,
            fallback_after_seconds=fallback_after,
        )
    raise ValueError(f"Unknown LLM provider: {name!r}. Supported: mistral, google-aistudio")


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
