from pathlib import Path
import click


def book_root(file_path: Path) -> Path:
    """Walk up from file_path until we find manifest.yaml.

    manifest.yaml always sits at the true book root (created once by `init`
    and never nested under a system folder), unlike source/, which now lives
    under s1b/ alongside every other system-produced folder.
    """
    current = file_path.resolve().parent
    while current != current.parent:
        if (current / "manifest.yaml").exists():
            return current
        current = current.parent
    raise click.ClickException(f"Cannot determine book root for: {file_path}")


def stage_output_dir(input_file: Path, book_root: Path, system: str, output_stage: str) -> Path:
    """Return output directory for a task, nested under the VSM system that
    produces it (mirroring the CLI's own s1b/s1d grouping), and mirroring
    any language subdirectory found in the input path.

    e.g. input at s1b/translated/es/file.txt, system="s1b" → s1b/<output_stage>/es/
         input at s1b/cleaned/file.txt,       system="s1b" → s1b/<output_stage>/
         input at s1b/copyedit/es/file.txt,   system="s1d" → s1d/<output_stage>/es/ (cross-system chaining)
    """
    rel = input_file.relative_to(book_root)
    parts = rel.parts
    if len(parts) >= 4:  # producing_system/stage/lang/file.txt
        return book_root / system / output_stage / parts[-2]
    return book_root / system / output_stage


def load_prompt(path: str) -> str:
    p = Path(path)
    if not p.exists():
        raise click.ClickException(f"Prompt file not found: {path}")
    content = p.read_text(encoding="utf-8")
    # Strip comment lines (lines starting with #)
    stripped = "\n".join(
        line for line in content.splitlines() if not line.startswith("#")
    ).strip()
    if not stripped:
        raise click.ClickException(
            f"Prompt file is empty: {path}\n"
            f"Add your system prompt there, or see prompts/examples/ for reference."
        )
    return stripped
