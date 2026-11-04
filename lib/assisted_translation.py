"""Assisted translation (ADR 024): the pipeline prepares chunk files, a person has
Claude Code translate them in the VS Code chat, and the pipeline validates and
merges the result. Nothing here calls a model.

Work folder, next to the merged output (`<book>/s1b/translated/es/assisted/`):

    chunks.yaml        index: source hash, chunk size, per-chunk paragraph/char counts
    glossary.md        per-book terms, kept up to date by the chat while it translates
    en/001.txt         source chunks, never edited
    es/001.txt         their translations, written in the chat (absent until done)
    es/001.rejected.txt   a translation that failed validation, moved aside by merge

Sources and translations live in separate folders so that `es/` shows at a glance
what has been translated, and the chat never writes next to a source file.

`run()` is what `engines/translation.py` calls when `translation.provider` is
`assisted`. It either raises `TaskWaiting` (prepared / still translating /
validation failed) or merges and returns the output path.
"""
import hashlib
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml

from lib.chunker import chunk_by_paragraphs
from lib.task_loader import TaskWaiting

WORK_DIRNAME = "assisted"
INDEX_NAME = "chunks.yaml"
GLOSSARY_NAME = "glossary.md"
PROMPT_PATH = "prompts/s1b/translate_task.txt"

DEFAULT_MAX_CHARS = 7000

# Length of a translation relative to its source chunk. Spanish runs longer than
# English (eTranslation's output for the first real book was 9% longer); outside
# this range something was dropped, repeated or padded. Chunks shorter than
# _MIN_CHECKED_CHARS (a lone chapter title) are not length-checked.
MIN_LENGTH_RATIO = 0.9
MAX_LENGTH_RATIO = 1.6
_MIN_CHECKED_CHARS = 200

_MARKUP = re.compile(r"\[/?i\]|\[/?sc\]|\[FN:|\[/FN\]\]")
_PREAMBLE_STARTS = ("aquí está", "aquí tienes", "aqui esta", "aqui tienes", "claro", "por supuesto",
                    "traducción:", "traduccion:", "here is", "here's", "sure")

_GLOSSARY_HEADER = ("# Glosario del libro\n"
                    "# Un término por línea, en este formato:  inglés → español\n")


# -- paths and the index -------------------------------------------------------

def work_dir(output_dir: Path) -> Path:
    return output_dir / WORK_DIRNAME


SOURCE_DIRNAME = "en"
TRANSLATION_DIRNAME = "es"


def _name(index: dict, n: int, suffix: str = "") -> str:
    return f"{n:0{index['width']}d}{suffix}.txt"


def source_path(work: Path, index: dict, n: int) -> Path:
    return work / SOURCE_DIRNAME / _name(index, n)


def translation_path(work: Path, index: dict, n: int) -> Path:
    return work / TRANSLATION_DIRNAME / _name(index, n)


def rejected_path(work: Path, index: dict, n: int) -> Path:
    return work / TRANSLATION_DIRNAME / _name(index, n, ".rejected")


def load_index(work: Path):
    path = work / INDEX_NAME
    if not path.is_file():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:  # LF on every platform
        f.write(text)


def paragraphs(text: str) -> list:
    return [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]


# -- prepare ---------------------------------------------------------------------

def prepare(work: Path, input_file: Path, root: Path, max_chars: int) -> dict:
    """Split `input_file` the way every chunked task does and write the work folder."""
    chunks = chunk_by_paragraphs(input_file.read_text(encoding="utf-8"), max_chars=max_chars)
    width = max(3, len(str(len(chunks))))
    index = {
        "source": str(input_file.relative_to(root)),
        "source_sha256": _sha256(input_file),
        "max_chars": max_chars,
        "width": width,
        "count": len(chunks),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "chunks": [{"n": n, "chars": len(c), "paragraphs": len(paragraphs(c))} for n, c in enumerate(chunks, 1)],
    }
    (work / SOURCE_DIRNAME).mkdir(parents=True, exist_ok=True)
    (work / TRANSLATION_DIRNAME).mkdir(exist_ok=True)
    for n, chunk in enumerate(chunks, 1):
        _write(source_path(work, index, n), chunk)
    glossary = work / GLOSSARY_NAME
    if not glossary.exists():
        _write(glossary, _GLOSSARY_HEADER)
    _write(work / INDEX_NAME, yaml.safe_dump(index, sort_keys=False, allow_unicode=True))
    return index


# -- progress ----------------------------------------------------------------------

def pending(work: Path, index: dict) -> list:
    """Chunk numbers that have no translation file yet (a rejected one counts as pending)."""
    return [n for n in range(1, index["count"] + 1) if not translation_path(work, index, n).exists()]


def source_changed(index: dict, input_file: Path) -> bool:
    return _sha256(input_file) != index["source_sha256"]


def _ranges(numbers: list) -> str:
    """[1, 2, 3, 7, 9, 10] -> '1-3, 7, 9-10'."""
    out, start = [], None
    for i, n in enumerate(numbers):
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append((start, prev))
            start = prev = n
        if i == len(numbers) - 1:
            out.append((start, prev))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in out)


def progress_text(work: Path, index: dict) -> str:
    missing = pending(work, index)
    return f"{index['count'] - len(missing)}/{index['count']}"


# -- validation --------------------------------------------------------------------

def validate_chunk(source: str, translated: str) -> list:
    """Why `translated` cannot be trusted as the translation of `source` (empty list: it can)."""
    if not translated.strip():
        return ["the file is empty"]
    problems = []

    src_paras, tr_paras = paragraphs(source), paragraphs(translated)
    if len(src_paras) != len(tr_paras):
        problems.append(f"{len(tr_paras)} paragraphs, the source has {len(src_paras)}")
    else:
        src_dividers = {i for i, p in enumerate(src_paras) if p.strip() == "---"}
        tr_dividers = {i for i, p in enumerate(tr_paras) if p.strip() == "---"}
        if src_dividers != tr_dividers:
            problems.append("the '---' dividers are not where they are in the source")

    if len(source) >= _MIN_CHECKED_CHARS:
        ratio = len(translated) / len(source)
        if not MIN_LENGTH_RATIO <= ratio <= MAX_LENGTH_RATIO:
            problems.append(f"length is {ratio:.0%} of the source "
                            f"(expected {MIN_LENGTH_RATIO:.0%}-{MAX_LENGTH_RATIO:.0%})")
        if translated.strip() == source.strip():
            problems.append("identical to the source (not translated)")

    src_markup, tr_markup = Counter(_MARKUP.findall(source)), Counter(_MARKUP.findall(translated))
    if src_markup != tr_markup:
        diffs = [f"{tok} x{tr_markup[tok]} (source x{src_markup[tok]})"
                 for tok in sorted(set(src_markup) | set(tr_markup)) if src_markup[tok] != tr_markup[tok]]
        problems.append("markup differs: " + ", ".join(diffs))

    first = translated.strip().splitlines()[0].strip().lower()
    if first.startswith(_PREAMBLE_STARTS) and not source.strip().lower().startswith(_PREAMBLE_STARTS):
        problems.append(f"starts with a chat preamble ({translated.strip().splitlines()[0][:40]!r})")
    if translated.count("```") > source.count("```"):
        problems.append("contains a code fence that is not in the source")
    heads = lambda t: sum(1 for line in t.splitlines() if line.startswith("#"))
    if heads(translated) != heads(source):
        problems.append("has Markdown '#' headings that the source does not")
    return problems


def validate_all(work: Path, index: dict) -> dict:
    """{chunk number: [problems]} for every translated chunk that fails; {} when all pass."""
    failures = {}
    for n in range(1, index["count"] + 1):
        problems = validate_chunk(source_path(work, index, n).read_text(encoding="utf-8"),
                                  translation_path(work, index, n).read_text(encoding="utf-8"))
        if problems:
            failures[n] = problems
    return failures


def reject(work: Path, index: dict, numbers: list) -> None:
    """Move failing translations aside so they count as pending and are redone."""
    for n in numbers:
        target = rejected_path(work, index, n)
        target.unlink(missing_ok=True)
        translation_path(work, index, n).replace(target)


def merged_text(work: Path, index: dict) -> str:
    return "\n\n".join(translation_path(work, index, n).read_text(encoding="utf-8").strip()
                       for n in range(1, index["count"] + 1))


# -- the task ------------------------------------------------------------------------

def _request(work: Path, root: Path) -> str:
    try:
        shown = work.relative_to(Path.cwd())
    except ValueError:
        shown = work
    return (f"In the VS Code chat, ask:\n"
            f"    Translate every pending chunk in {shown} following {PROMPT_PATH}.\n"
            f"Then run `s2 run {root.name}` again (or `s1b translate` on the cleaned file) to validate and merge.")


def _settings(config: dict) -> dict:
    settings = config["translation"].get("assisted") or {}
    max_chars = settings.get("max_chars", DEFAULT_MAX_CHARS)
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 500:
        raise click.ClickException("translation.assisted.max_chars must be a whole number of at least 500.")
    return {"max_chars": max_chars, "model_note": str(settings.get("model_note") or "")}


def run(input_file: Path, root: Path, output_dir: Path, output_file: Path, config: dict,
        *, status: bool = False, restart: bool = False):
    """One call of the assisted `translate` task. Returns (output_file, metrics) once merged;
    otherwise raises TaskWaiting. `status=True` only reports (nothing written, ledger untouched)."""
    settings = _settings(config)
    work = work_dir(output_dir)
    index = load_index(work)

    if status:
        if index is None:
            raise TaskWaiting("Not prepared yet: run `s1b translate` (or `s2 run`) to split the text into chunks.",
                              record=False)
        missing = pending(work, index)
        note = "  WARNING: the cleaned source changed since the chunks were made; use --restart." \
            if source_changed(index, input_file) else ""
        if missing:
            raise TaskWaiting(f"{progress_text(work, index)} chunks translated; pending: {_ranges(missing)}.{note}\n"
                              f"{_request(work, root)}", record=False)
        raise TaskWaiting(f"All {index['count']} chunks are translated; run `s1b translate` to validate and merge.{note}",
                          record=False)

    if restart and work.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        work.rename(work.with_name(f"{WORK_DIRNAME}.old-{stamp}"))
        index = None
        click.echo(f"  Previous work folder kept as {WORK_DIRNAME}.old-{stamp}")

    output_dir.mkdir(parents=True, exist_ok=True)

    if index is None:
        index = prepare(work, input_file, root, settings["max_chars"])
        click.echo(f"  Prepared {index['count']} chunks of up to {index['max_chars']} characters in {work}")
        raise TaskWaiting(f"0/{index['count']} chunks translated.\n{_request(work, root)}",
                          progress=f"0/{index['count']}", work_dir=str(work.relative_to(root)))

    if source_changed(index, input_file):
        raise click.ClickException(
            f"{input_file.name} changed since the chunks in {work} were made (cleanup was probably re-run). "
            "Translating against the old text would undo the new cleanup: run `s1b translate --restart` "
            "to split the new text (the old work folder is kept, not deleted).")

    missing = pending(work, index)
    if missing:
        click.echo(f"  {progress_text(work, index)} chunks translated.")
        raise TaskWaiting(f"{progress_text(work, index)} chunks translated; pending: {_ranges(missing)}.\n"
                          f"{_request(work, root)}",
                          progress=progress_text(work, index), work_dir=str(work.relative_to(root)))

    failures = validate_all(work, index)
    if failures:
        reject(work, index, list(failures))
        lines = [f"  chunk {n}: {'; '.join(problems)}" for n, problems in failures.items()]
        click.echo("\n".join(lines))
        raise TaskWaiting(
            f"{len(failures)} translated chunk(s) failed validation and were moved aside "
            f"(es/NNN.rejected.txt): {_ranges(list(failures))}.\n{_request(work, root)}",
            progress=f"{index['count'] - len(failures)}/{index['count']}", work_dir=str(work.relative_to(root)))

    text = merged_text(work, index)
    output_file.write_text(text, encoding="utf-8")
    click.echo(f"  Merged {index['count']} chunks.")
    metrics = {"usage": {"characters": len(text)}}
    if settings["model_note"]:
        metrics["model"] = settings["model_note"]
    return output_file, metrics
