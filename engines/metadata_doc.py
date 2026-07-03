"""Assembles a book's metadata document from marketing_metadata.yaml facts plus
the brief's <overview> tags — no LLM call, since bibliographic facts should
never be guessed and a running description is already fully written by the
`brief` task.
"""
import re
from pathlib import Path

import click

from lib import manifest, metadata_blocks, paths

_OVERVIEW_RE = re.compile(r"<overview>\s*(.*?)\s*</overview>", re.DOTALL)


def run(input_file: Path, root: Path, system: str, output_name: str, config: dict,
        *, metadata_config: str) -> Path:
    data = metadata_blocks.load(metadata_config)

    brief_text = input_file.read_text(encoding="utf-8")
    overviews = _OVERVIEW_RE.findall(brief_text)
    if overviews:
        description = "\n\n".join(o.strip() for o in overviews)
    else:
        description = "(No se encontró ninguna etiqueta <overview> en el brief.)"
        click.echo("  Warning: no <overview> tag found in the brief; description left as a placeholder.")

    output_dir = paths.stage_output_dir(input_file, root, system, output_name)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / input_file.name

    parts = ["## Descripción", "", description]
    for key, facts in data.items():
        heading = key.replace("_", " ").title()
        parts += ["", f"## {heading}", "", metadata_blocks.render_block(facts)]

    output_file.write_text("\n".join(parts).strip() + "\n", encoding="utf-8")

    manifest.update(
        root,
        **{f"{output_name}_{output_dir.name}": str(output_file.relative_to(root))},
    )

    click.echo(f"Saved: {output_file}")
    return output_file
