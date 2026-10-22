"""Files the UI opens or creates: opening in the OS default application, the
`Saved:` paths a command prints. Plain Python, no Textual.

The UI renders no deliverables (ADR 023, Decision 7); everything is opened
externally with `click.launch()`, which works on Windows, macOS and Linux.
"""
from pathlib import Path

import click

_SAVED_PREFIX = "Saved: "


def open_path(path) -> None:
    """Open a file or folder in the operating system's default application."""
    click.launch(str(path))


def parse_saved(lines: list) -> list:
    """The paths of every `Saved: <path>` line a command printed, in order."""
    return [Path(line[len(_SAVED_PREFIX):].strip()) for line in lines if line.startswith(_SAVED_PREFIX)]
