import click
from pathlib import Path

from lib.chunker import chunk_by_paragraphs
from lib import manifest
from providers import get_translation_provider


def run(input_file: Path, root: Path, system: str, output_name: str, config: dict) -> Path:
    source_lang = config["translation"].get("source_lang", "EN")
    target_lang = config["translation"].get("target_lang", "ES")
    lang_dir = target_lang.lower()

    output_dir = root / system / output_name / lang_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / input_file.name

    translator = get_translation_provider(config)

    raw_text = input_file.read_text(encoding="utf-8")
    # DeepL accepts up to 128 KB per request; chunk conservatively to stay safe
    chunks = chunk_by_paragraphs(raw_text, max_chars=50000)
    total = len(chunks)

    parts = []
    for i, chunk in enumerate(chunks, 1):
        click.echo(f"  Translating chunk {i}/{total}...")
        parts.append(translator.translate(chunk, source_lang, target_lang))

    output_file.write_text("\n\n".join(parts), encoding="utf-8")

    manifest.update(
        root,
        **{
            f"{output_name}_{lang_dir}": str(output_file.relative_to(root)),
            "translation_provider": config["translation"]["provider"],
            "translation_pair": f"{source_lang}→{target_lang}",
        },
    )

    click.echo(f"Saved: {output_file}")
    return output_file, {"usage": translator.usage}
