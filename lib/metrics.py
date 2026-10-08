"""System 3 — performance-monitoring metrics capture. See ADR 005.

Turns the raw usage a provider-calling engine reports (token or character
counts) into ledger-ready fields: provider, model, the raw usage dict, and
an optional cost in USD. Cost is only computed when the corresponding
`pricing:` block is present in config.yaml — prices change over time and by
account tier, so this project never hardcodes a dollar figure per model.
"""


def enrich(raw_metrics: dict, config: dict) -> dict:
    usage = raw_metrics.get("usage")
    if not usage:
        return {}

    if "total_tokens" in usage:
        fields = {
            "provider": config["llm"]["provider"],
            # ADR 020: the engine reports the model that actually served
            # requests (differs from config only after a fallback switch).
            "model": raw_metrics.get("model") or config["llm"].get("model"),
            "usage": usage,
            "cost_usd": _llm_cost(usage, config["llm"].get("pricing")),
        }
        if raw_metrics.get("models_used"):
            fields["models_used"] = raw_metrics["models_used"]
        return fields

    return {  # character-based usage (translation)
        "provider": config["translation"]["provider"],
        "usage": usage,
        "cost_usd": _translation_cost(usage, _translation_pricing(config["translation"])),
    }


def _translation_pricing(translation_config: dict):
    """Pricing is per provider (ADR 022, Decision 5): a single top-level block
    is provider-blind, so after a provider switch it would keep charging the
    old provider's rate. The legacy top-level `translation.pricing` is still
    honored, but only for DeepL — the only provider it was ever written for."""
    provider = translation_config["provider"]
    nested = (translation_config.get(provider) or {}).get("pricing")
    if nested:
        return nested
    return translation_config.get("pricing") if provider == "deepl" else None


def _llm_cost(usage: dict, pricing: dict) -> float:
    if not pricing:
        return None
    return round(
        usage.get("prompt_tokens", 0) / 1_000_000 * pricing.get("prompt_per_million", 0)
        + usage.get("completion_tokens", 0) / 1_000_000 * pricing.get("completion_per_million", 0),
        6,
    )


def _translation_cost(usage: dict, pricing: dict) -> float:
    if not pricing:
        return None
    return round(usage.get("characters", 0) / 1_000_000 * pricing.get("per_million_characters", 0), 6)
