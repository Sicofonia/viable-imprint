"""System 1D's `newsletter-track` task: extracts which explorer and dish were
featured in this issue, checks each against the non-repetition tracking
files (warns, doesn't fail, on a repeat — advisory, human-reviewed before
sending), records new entries, and writes the final tag-stripped copy ready
to send. No LLM call here — the check is deterministic. See
docs/adr/008-system1d-newsletter.md, point 3.
"""
import re
from datetime import date
from pathlib import Path

import click
import yaml

from lib import paths

_EXPLORER_RE = re.compile(r"<explorer>\s*(.*?)\s*</explorer>", re.DOTALL)
_DISH_RE = re.compile(r"<dish>\s*(.*?)\s*</dish>", re.DOTALL)


def run(input_file: Path, root: Path, system: str, output_name: str, config: dict) -> Path:
    text = input_file.read_text(encoding="utf-8")
    explorer = _extract(_EXPLORER_RE, text)
    dish = _extract(_DISH_RE, text)

    warnings = []
    warnings += _check_and_record(root / "featured_explorers.yaml", explorer, "explorer", "<explorer>")
    warnings += _check_and_record(root / "featured_dishes.yaml", dish, "dish", "<dish>")

    clean_text = _EXPLORER_RE.sub(lambda m: m.group(1), text)
    clean_text = _DISH_RE.sub(lambda m: m.group(1), clean_text)

    output_dir = paths.stage_output_dir(input_file, root, system, output_name)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / input_file.name
    output_file.write_text(clean_text, encoding="utf-8")

    for w in warnings:
        click.echo(f"  Warning: {w}")
    click.echo(f"Saved: {output_file}")
    return output_file


def _extract(pattern: re.Pattern, text: str) -> str:
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def _check_and_record(path: Path, value: str, label: str, tag: str) -> list:
    if not value:
        return [f"No {tag} tag found — {label}-of-the-month tracking not updated."]

    entries = _load_list(path)
    if any(e.get("name", "").strip().lower() == value.lower() for e in entries):
        return [f"'{value}' appears to repeat a previous {label} of the month — check before sending."]

    entries.append({"name": value, "date_featured": date.today().isoformat()})
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(entries, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return []


def _load_list(path: Path) -> list:
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or []
    except yaml.YAMLError:
        click.echo(f"  Warning: {path} is corrupted — treating this list as empty.")
        return []
