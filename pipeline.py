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
  python pipeline.py s2 status life-as-explorer
  python pipeline.py s2 run life-as-explorer

Output always lands under the VSM system that produced it (s1b/, s1d/, ...),
mirroring this CLI's own nested command structure — only manifest.yaml stays
at the book root, since it's shared across every system.
"""
import sys
from pathlib import Path

import click
import yaml
from dotenv import load_dotenv

from lib import orchestrator
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
cli.add_command(build_system_group("s4", "System 4 — Strategic Intelligence"))


# ---------------------------------------------------------------------------
# System 2 — Coordination. Unlike s1b/s1d/s4, this group is hand-written, not
# manifest-driven: System 2 coordinates *across* systems rather than
# declaring its own tasks. See docs/adr/004-system-2-run-orchestration.md.
# ---------------------------------------------------------------------------

def _resolve_book_root(config: dict, book_slug: str) -> Path:
    root = (Path(config.get("books_dir", "books")) / book_slug).resolve()
    if not (root / "manifest.yaml").exists():
        raise click.ClickException(
            f"No manifest.yaml found at {root}. Run `pipeline.py init {book_slug}` first."
        )
    return root


_SYSTEM_LABELS = {
    "s1b": "System 1B — Editorial Production",
    "s1d": "System 1D — Publication and Marketing",
}


@click.group(name="s2", help="System 2 — Coordination (run-state and task orchestration)")
def s2():
    pass


@s2.command(name="status")
@click.argument("book_slug")
@click.pass_context
def s2_status(ctx, book_slug):
    """Show every book-scoped task's status: done, ready, blocked, or failed."""
    root = _resolve_book_root(ctx.obj["config"], book_slug)
    click.echo(f"Book: {book_slug} ({root})")

    current_system = None
    for row in orchestrator.book_status(root):
        if row["system"] != current_system:
            current_system = row["system"]
            click.echo(f"\n{_SYSTEM_LABELS[current_system]}")

        name = row["name"]
        if row["status"] == "done":
            click.echo(f"  [x] {name:<20} {row['output']}")
        elif row["status"] == "ready":
            click.echo(f"  [ ] {name:<20} ready")
        elif row["status"] == "failed":
            retry = "ready to retry" if row["ready_to_retry"] else "blocked"
            click.echo(f"  [!] {name:<20} failed ({retry}) — {row['error']}")
        else:
            click.echo(f"  [ ] {name:<20} blocked — {row['reason']}")


@s2.command(name="run")
@click.argument("book_slug")
@click.option("--only", type=click.Choice(["s1b", "s1d"]), default=None,
              help="Restrict to one system.")
@click.option("--step", is_flag=True, help="Run exactly one ready task and stop.")
@click.pass_context
def s2_run(ctx, book_slug, only, step):
    """Run every currently-ready task, across systems, until nothing more is unblocked."""
    config = ctx.obj["config"]
    root = _resolve_book_root(config, book_slug)
    summary = orchestrator.run_book(root, config, only=only, step=step)

    click.echo(f"\nDone: {len(summary['done'])}, Failed: {len(summary['failed'])}")
    for label in summary["done"]:
        click.echo(f"  [x] {label}")
    for label in summary["failed"]:
        click.echo(f"  [!] {label}")
    if summary["complete"]:
        click.echo("Book complete — nothing left to run.")
    if summary["failed"]:
        sys.exit(1)


cli.add_command(s2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()
