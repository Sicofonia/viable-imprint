"""Files the UI opens or creates: opening in the OS default application, the
`Saved:` paths a command prints, and the few operator inputs the UI writes for
them. Plain Python, no Textual.

The UI renders no deliverables (ADR 023, Decision 7); everything is opened
externally with `click.launch()`, which works on Windows, macOS and Linux. The
only files the UI writes itself are operator inputs a human would otherwise
create by hand: a new book's manuscript copy, an empty candidate brief, this
month's newsletter notes file.
"""
import re
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import click

_SAVED_PREFIX = "Saved: "


def open_path(path) -> None:
    """Open a file or folder in the operating system's default application."""
    click.launch(str(path))


def parse_saved(lines: list) -> list:
    """The paths of every `Saved: <path>` line a command printed, in order."""
    return [Path(line[len(_SAVED_PREFIX):].strip()) for line in lines if line.startswith(_SAVED_PREFIX)]


# -- a new book (T2.1) ---------------------------------------------------------

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def slug_problem(ctx, slug: str):
    """Why `slug` cannot name a new book, or None."""
    if not slug:
        return "Enter a name for the book."
    if not SLUG_RE.match(slug):
        return "Use lowercase letters, digits and single hyphens only (for example life-as-explorer)."
    if (ctx.data_dir("books_dir") / slug).exists():
        return f"A book called {slug!r} already exists."
    return None


def source_problem(source: str):
    """Why `source` cannot be the manuscript, or None."""
    if not source:
        return "Choose the manuscript .txt file."
    path = Path(source)
    if not path.is_file():
        return f"Not a file: {path}"
    if path.suffix.lower() != ".txt":
        return "The manuscript must be a .txt file."
    return None


def copy_source(ctx, slug: str, source) -> Path:
    """Copy the manuscript into `<book>/s1b/source/`. Refuses if a file is already there."""
    folder = ctx.data_dir("books_dir") / slug / "s1b" / "source"
    present = [f for f in folder.iterdir() if f.is_file()] if folder.is_dir() else []
    if present:
        raise FileExistsError(f"{folder} already holds {present[0].name}; the pipeline expects exactly one file there.")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / Path(source).name
    shutil.copy2(source, target)
    return target


# -- dated run folders (T5.1) --------------------------------------------------------
# S4 and the outbound articles write `<root>/<system>/<stage>/<YYYY-MM-DD>/combined.txt`
# (ADR 003, ADR 012). The date is the folder name, so "newest" is the largest name.

@dataclass
class DatedOutput:
    date: str
    path: Path


def dated_outputs(root: Path, system: str, stage: str) -> list:
    """Every finished run of a dated stage, newest first. A folder without the output
    (a run that failed before writing it) and non-folders such as `_state.yaml` are skipped."""
    base = Path(root) / system / stage
    if not base.is_dir():
        return []
    found = [DatedOutput(d.name, d / "combined.txt") for d in base.iterdir()
             if d.is_dir() and (d / "combined.txt").is_file()]
    return sorted(found, key=lambda o: o.date, reverse=True)


# -- this month's newsletter notes (T6.2) ---------------------------------------------
# `newsletter-scan` reads `<newsletter_dir>/notes/YYYY-MM.txt` (ADR 008). The operator writes
# it by hand; the UI only creates it empty so there is a file to open in the editor.

def today() -> date:
    """Today's date. A function so tests can freeze it."""
    return date.today()


def this_month() -> str:
    return today().isoformat()[:7]


def notes_path(ctx) -> Path:
    return ctx.data_dir("newsletter_dir") / "notes" / f"{this_month()}.txt"


def notes_state(ctx) -> str:
    """"missing", "empty" (nothing but whitespace, which `newsletter-scan` treats as none) or "ready".
    Looking creates nothing."""
    path = notes_path(ctx)
    if not path.is_file():
        return "missing"
    return "ready" if path.read_text(encoding="utf-8").strip() else "empty"


def ensure_notes_file(ctx) -> Path:
    """This month's notes file, created empty if it does not exist. Never overwrites."""
    path = notes_path(ctx)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return path
