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


def intelligence_root(config: dict) -> Path:
    """Return System 4's perpetual project folder, creating it (and its
    manifest.yaml) on first use — the equivalent of `init` for a book, but
    automatic, since there's no per-title identity to name it after.
    """
    root = Path(config.get("intelligence_dir", "intelligence")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.yaml"
    if not manifest_path.exists():
        manifest_path.write_text("slug: intelligence\n", encoding="utf-8")
    return root


def candidates_root(config: dict) -> Path:
    """Return System 5's perpetual candidates-under-evaluation folder,
    creating it (and its manifest.yaml) on first use — same shape as
    `intelligence_root()`, for the same reason: a candidate text has no
    editorial commitment behind it yet, so it doesn't belong under books_dir.
    See docs/adr/006-system-5-policy-agent.md.
    """
    root = Path(config.get("candidates_dir", "candidates")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.yaml"
    if not manifest_path.exists():
        manifest_path.write_text("slug: candidates\n", encoding="utf-8")
    return root


def homeostat_root(config: dict) -> Path:
    """Return System 5's perpetual homeostat-dashboard folder, creating it
    (and its manifest.yaml) on first use — same shape as `intelligence_root()`
    and `candidates_root()`. See docs/adr/007-system5-homeostat-dashboard.md.
    """
    root = Path(config.get("homeostat_dir", "homeostat")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.yaml"
    if not manifest_path.exists():
        manifest_path.write_text("slug: homeostat\n", encoding="utf-8")
    return root


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
    lines = p.read_text(encoding="utf-8").splitlines()

    # Only a *leading* block of "# ..." lines is a maintainer comment (e.g.
    # the sync-reminder atop prompts/s5/policy_evaluation_task.txt, or the
    # usage notes atop every prompts/examples/ file) — stop stripping at the
    # first line that isn't blank and doesn't start with "#". A "#"/"##"
    # appearing later in the file is the prompt's own literal Markdown
    # heading, part of the fill-in-the-template contract (ADR 002 point 6),
    # and must reach the model untouched. The previous version of this
    # function stripped every "#"-prefixed line anywhere in the file, which
    # silently deleted every such heading from every fill-in-template prompt
    # project-wide — found while building ADR 007's homeostat prompt, whose
    # headings were being dropped for exactly this reason. See that ADR's
    # implementation notes.
    i = 0
    while i < len(lines) and (lines[i].startswith("#") or not lines[i].strip()):
        i += 1
    stripped = "\n".join(lines[i:]).strip()

    if not stripped:
        raise click.ClickException(
            f"Prompt file is empty: {path}\n"
            f"Add your system prompt there, or see prompts/examples/ for reference."
        )
    return stripped
