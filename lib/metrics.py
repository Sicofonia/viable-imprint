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
        return {
            "provider": config["llm"]["provider"],
            "model": config["llm"].get("model"),
            "usage": usage,
            "cost_usd": _llm_cost(usage, config["llm"].get("pricing")),
        }

    return {  # character-based usage (translation)
        "provider": config["translation"]["provider"],
        "usage": usage,
        "cost_usd": _translation_cost(usage, config["translation"].get("pricing")),
    }


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
