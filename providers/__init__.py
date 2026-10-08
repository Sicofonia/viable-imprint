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
            "'glm-4.7-flash' for Z.ai, 'mistral-medium-latest' for Mistral, "
            "'gemini-3.6-flash' for Google AI Studio)."
        )
    if name == "mistral":
        api_key = os.environ.get("MISTRAL_API_KEY")
        if not api_key:
            raise click.ClickException(
                "MISTRAL_API_KEY is not set. Add it to your .env file."
            )
        from providers.llm.mistral import MistralProvider
        # Optional; unset keeps Mistral's original back-to-back behavior (0).
        pacing = config["llm"].get("request_pacing_seconds", 0.0)
        if isinstance(pacing, bool) or not isinstance(pacing, (int, float)) or pacing < 0:
            raise click.ClickException("llm.request_pacing_seconds must be a non-negative number of seconds.")
        return MistralProvider(
            api_key=api_key,
            model=model,
            temperature=config["llm"].get("temperature", 0.0),
            request_pacing_seconds=pacing,
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
    if name == "z-ai":
        api_key = os.environ.get("ZAI_API_KEY")
        if not api_key:
            raise click.ClickException(
                "ZAI_API_KEY is not set. Add it to your .env file."
            )
        # ADR 021: Z.ai-only, ignored by the other providers.
        thinking = config["llm"].get("thinking", "disabled")
        if thinking not in ("enabled", "disabled"):
            raise click.ClickException("llm.thinking must be 'enabled' or 'disabled' (Z.ai only).")
        from providers.llm.zai import ZaiProvider, _DEFAULT_REQUEST_PACING_SECONDS
        pacing = config["llm"].get("request_pacing_seconds", _DEFAULT_REQUEST_PACING_SECONDS)
        if isinstance(pacing, bool) or not isinstance(pacing, (int, float)) or pacing < 0:
            raise click.ClickException("llm.request_pacing_seconds must be a non-negative number of seconds.")
        return ZaiProvider(
            api_key=api_key,
            model=model,
            temperature=config["llm"].get("temperature", 0.0),
            thinking=thinking,
            request_pacing_seconds=pacing,
        )
    raise ValueError(f"Unknown LLM provider: {name!r}. Supported: z-ai, mistral, google-aistudio")


def get_translation_provider(config: dict) -> TranslationProvider:
    name = config["translation"]["provider"]
    if name == "etranslation":
        keys = ("ETRANSLATION_APP_NAME", "ETRANSLATION_PASSWORD", "ETRANSLATION_RECEIVER_SECRET")
        missing = [k for k in keys if not os.environ.get(k)]
        if missing:
            raise click.ClickException(f"{', '.join(missing)} not set. Add to your .env file.")
        settings = config["translation"].get("etranslation") or {}
        receiver_url = settings.get("receiver_url")
        if not isinstance(receiver_url, str) or not receiver_url.startswith("https://") or "?" in receiver_url:
            raise click.ClickException(
                "translation.etranslation.receiver_url must be set in config.yaml to the deployed "
                "receiver's https URL with no query string "
                "(e.g. https://<project>.vercel.app/api/etranslation) — see receivers/etranslation-vercel/README.md."
            )
        document_format = settings.get("document_format", "html")
        if document_format not in ("html", "txt"):
            raise click.ClickException("translation.etranslation.document_format must be 'html' or 'txt'.")
        llm_enhanced = settings.get("llm_enhanced", False)
        if llm_enhanced is not None and not isinstance(llm_enhanced, bool):
            raise click.ClickException("translation.etranslation.llm_enhanced must be true, false, or null (omit).")
        numbers = {}
        for key, default in (("poll_interval_seconds", 30.0), ("timeout_minutes", 120.0)):
            value = settings.get(key, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise click.ClickException(f"translation.etranslation.{key} must be a positive number.")
            numbers[key] = float(value)
        from providers.translation.etranslation import ETranslationProvider
        return ETranslationProvider(
            app_name=os.environ["ETRANSLATION_APP_NAME"],
            password=os.environ["ETRANSLATION_PASSWORD"],
            receiver_url=receiver_url,
            receiver_secret=os.environ["ETRANSLATION_RECEIVER_SECRET"],
            domain=settings.get("domain", "GEN"),
            document_format=document_format,
            llm_enhanced=llm_enhanced,
            **numbers,
        )
    if name == "deepl":
        api_key = os.environ.get("DEEPL_API_KEY")
        if not api_key:
            raise click.ClickException(
                "DEEPL_API_KEY is not set. Add it to your .env file."
            )
        from providers.translation.deepl import DeepLProvider
        return DeepLProvider(api_key=api_key)
    raise ValueError(f"Unknown translation provider: {name!r}. Supported: etranslation, deepl")
