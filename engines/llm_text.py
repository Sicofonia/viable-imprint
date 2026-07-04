import re
from pathlib import Path

import click

from lib.chunker import chunk_by_paragraphs
from lib import manifest, metadata_blocks, paths
from providers import get_llm_provider

# Occasionally observed across markdown-heading prompts: the model prefixes a
# correct heading with a spurious extra heading marker of its own, e.g.
# "# ## Biografía del autor" or "### ### La puerta norte..." instead of just
# "## Biografía del autor" / "### La puerta norte...". Prompt wording alone
# didn't eliminate this reliably across repeated runs, so it's normalized
# here instead — safe to apply unconditionally, since no legitimate line
# ever starts with two consecutive heading markers.
_DOUBLED_HEADING_RE = re.compile(r"^#{1,6} (?=#{1,6} )", re.MULTILINE)


def _normalize_headings(text: str) -> str:
    while _DOUBLED_HEADING_RE.search(text):
        text = _DOUBLED_HEADING_RE.sub("", text)
    return text


def run(input_file: Path, root: Path, system: str, output_name: str, config: dict,
        *, prompt: str, manifest_key: str = None, max_chars: int = 8000,
        temperature: float = None, metadata_config: str = None,
        metadata_footer: str = None) -> Path:
    output_dir = paths.stage_output_dir(input_file, root, system, output_name)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / input_file.name

    system_prompt = paths.load_prompt(prompt)
    # Loaded up front so a missing/misconfigured file fails before any LLM calls.
    metadata = metadata_blocks.load(metadata_config) if metadata_config else None
    llm = get_llm_provider(config)

    raw_text = input_file.read_text(encoding="utf-8")
    chunks = chunk_by_paragraphs(raw_text, max_chars=max_chars)
    total = len(chunks)

    parts = []
    for i, chunk in enumerate(chunks, 1):
        click.echo(f"  {output_name} chunk {i}/{total}...")
        parts.append(llm.complete(system_prompt, chunk, temperature=temperature))

    output_text = _normalize_headings("\n\n".join(parts))
    if metadata:
        output_text = metadata_blocks.substitute(output_text, metadata)
    if metadata_footer:
        output_text = metadata_blocks.append_footer(output_text, metadata, metadata_footer)
    output_file.write_text(output_text, encoding="utf-8")

    key = manifest_key or output_name
    existing = manifest.load(root)
    source_kwargs = {} if "source" in existing else {"source": str(input_file.relative_to(root))}
    manifest.update(
        root,
        **{key: str(output_file.relative_to(root))},
        llm_provider=config["llm"]["provider"],
        llm_model=config["llm"].get("model", "mistral-large-latest"),
        **source_kwargs,
    )

    click.echo(f"Saved: {output_file}")
    return output_file
