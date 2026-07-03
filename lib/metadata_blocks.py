"""Shared loading/rendering for per-book marketing metadata (templates/marketing_metadata.yaml).

The file groups facts into named blocks (e.g. book_facts, contact_facts), each
an ordered list of {label, value} pairs — ISBNs, prices, contact details:
things nobody should ask an LLM to guess or reliably remember to place.

Two ways a block reaches the output, depending on where it belongs:
- `substitute()` — for a block that lands at a content-dependent spot inside
  the LLM's own prose (e.g. right after a specific paragraph). The prompt
  leaves a literal {{block_name}} token there; this replaces it.
- `append_footer()` — for a block that always belongs at the very end of the
  document, regardless of what the LLM wrote. Relying on the model to
  remember a trailing token is unreliable in practice (it tends to end on
  whatever felt like the natural close, e.g. a quote) — appending it in code
  guarantees it's always there.
"""
import re
from pathlib import Path

import click
import yaml

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def load(metadata_config: str) -> dict:
    path = Path(metadata_config)
    if not path.exists():
        raise click.ClickException(
            f"Metadata config not found: {path}\n"
            "Copy templates/marketing_metadata.example.yaml to that path and "
            "fill in this book's facts."
        )
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def render_block(facts: list) -> str:
    return "\n".join(
        f"- **{item['label']}**: {item['value']}" for item in facts if item.get("value")
    )


def substitute(text: str, data: dict) -> str:
    for key, facts in data.items():
        placeholder = "{{" + key + "}}"
        if placeholder in text:
            text = text.replace(placeholder, render_block(facts))

    leftover = {m for m in _PLACEHOLDER_RE.findall(text) if m not in data}
    if leftover:
        click.echo(f"  Warning: unresolved metadata placeholders in output: {', '.join(sorted(leftover))}")

    return text


def append_footer(text: str, data: dict, key: str) -> str:
    if key not in data:
        raise click.ClickException(
            f"metadata_footer '{key}' not found in metadata config. "
            f"Available blocks: {', '.join(sorted(data)) or '(none)'}"
        )
    return text.rstrip("\n") + "\n\n" + render_block(data[key])
