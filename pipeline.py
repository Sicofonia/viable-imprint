#!/usr/bin/env python3
"""Viable Imprint — editorial pipeline CLI.

Tasks are loaded dynamically from each VSM system's tasks.yaml manifest
(systems/<system>/tasks.yaml). Adding, removing, or reordering a task within
a system requires editing that system's manifest and prompt files — no
Python changes needed for prompt-driven tasks.

Usage examples:
  python pipeline.py init life-as-explorer
  python pipeline.py s1b cleanup books/life-as-explorer/s1b/source/my-life.txt
  python pipeline.py s1b translate books/life-as-explorer/s1b/cleaned/my-life.txt
  python pipeline.py s1b ortho books/life-as-explorer/s1b/translated/es/my-life.txt
  python pipeline.py s1b copyedit books/life-as-explorer/s1b/ortho/es/my-life.txt
  python pipeline.py s1b format books/life-as-explorer/s1b/copyedit/es/my-life.txt
  python pipeline.py s1d brief books/life-as-explorer/s1b/copyedit/es/my-life.txt

Output always lands under the VSM system that produced it (s1b/, s1d/, ...),
mirroring this CLI's own nested command structure — only manifest.yaml stays
at the book root, since it's shared across every system.
"""
import sys
from pathlib import Path

import click
import yaml
from dotenv import load_dotenv

from lib.task_loader import build_system_group

load_dotenv()  # reads .env into os.environ before any provider is instantiated


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_config(config_path: str) -> dict:
    p = Path(config_path)
    if not p.exists():
        click.echo(
            f"Config file '{config_path}' not found. "
            "Copy config.example.yaml → config.yaml and fill in your API keys.",
            err=True,
        )
        sys.exit(1)
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
@click.option("--config", default="config.yaml", show_default=True,
              help="Path to config YAML file.")
@click.pass_context
def cli(ctx, config):
    ctx.ensure_object(dict)
    ctx.obj["config"] = _load_config(config)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("book_slug")
@click.pass_context
def init(ctx, book_slug):
    """Create a new book project (s1b/source/ folder + manifest.yaml).

    BOOK_SLUG is a short identifier used as the folder name, e.g. life-as-explorer.
    Every other folder is created automatically the first time a task writes
    to it, nested under whichever VSM system's task produced it (s1b/, s1d/,
    ...) — matching the CLI's own nested command structure. manifest.yaml
    stays at the book root, since it's shared across every system.
    """
    config = ctx.obj["config"]
    books_dir = Path(config.get("books_dir", "books"))
    book_root = books_dir / book_slug

    if book_root.exists():
        raise click.ClickException(f"Book directory already exists: {book_root}")

    (book_root / "s1b" / "source").mkdir(parents=True)
    (book_root / "manifest.yaml").write_text(
        f"slug: {book_slug}\nsource: ~\n", encoding="utf-8"
    )

    click.echo(f"Created: {book_root}/")
    click.echo(f"  Place your source .txt file in: {book_root / 's1b' / 'source'}/")


# ---------------------------------------------------------------------------
# System command groups — tasks are loaded dynamically from each system's
# tasks.yaml. Only systems with implemented tasks are registered here.
# ---------------------------------------------------------------------------

cli.add_command(build_system_group("s1b", "System 1B — Editorial Production"))
cli.add_command(build_system_group("s1d", "System 1D — Publication and Marketing"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()
