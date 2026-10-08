import click
from pathlib import Path

from lib.chunker import chunk_by_paragraphs
from lib import manifest
from providers import get_translation_provider

CLI_OPTIONS = [
    click.Option(
        ["--document", "-d"],
        is_flag=True,
        default=False,
        help="Use the provider's whole-document translation instead of "
             "this project's own paragraph-chunked text translation — no "
             "chunking, but [i]/[sc] markup is sent as literal text, not "
             "specially preserved (there's no tag_handling equivalent for "
             "document translation). Requires a provider that implements "
             "translate_document(); fails clearly if it doesn't.",
    ),
]


def run(input_file: Path, root: Path, system: str, output_name: str, config: dict,
        *, document: bool = False) -> Path:
    source_lang = config["translation"].get("source_lang", "EN")
    target_lang = config["translation"].get("target_lang", "ES")
    lang_dir = target_lang.lower()

    output_dir = root / system / output_name / lang_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / input_file.name

    translator = get_translation_provider(config)

    if document:
        translate_document = getattr(translator, "translate_document", None)
        if translate_document is None:
            raise click.ClickException(
                f"{config['translation']['provider']!r} doesn't support --document "
                "(no translate_document() method) — omit --document to use this "
                "project's own chunked text translation instead."
            )
        click.echo("  Translating whole document (no chunking)...")
        translate_document(input_file, output_file, source_lang, target_lang)
    else:
        raw_text = input_file.read_text(encoding="utf-8")
        # A request-size limit belongs to the provider that has it (ADR 022,
        # Decision 4); 50000 is only the fallback for one that declares none.
        chunks = chunk_by_paragraphs(raw_text, max_chars=getattr(translator, "max_chars_per_request", 50000))
        total = len(chunks)

        # Optional hooks for a provider whose translate() can outlive this process.
        begin_run = getattr(translator, "begin_run", None)
        if begin_run is not None:
            begin_run(output_dir, input_file.stem, f"{root.name}/{input_file.stem}")

        parts = []
        for i, chunk in enumerate(chunks, 1):
            click.echo(f"  Translating chunk {i}/{total}...")
            parts.append(translator.translate(chunk, source_lang, target_lang))

        output_file.write_text("\n\n".join(parts), encoding="utf-8")

        finish_run = getattr(translator, "finish_run", None)
        if finish_run is not None:
            finish_run()

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
