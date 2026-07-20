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
from datetime import date
from pathlib import Path

import click
import yaml
from dotenv import load_dotenv

from lib import dashboard, homeostat, orchestrator, paths
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

# s5 mixes manifest-driven tasks (evaluate, the homeostat trio) with one
# hand-written command (log-decision) — captured in a variable so the latter
# can be added before the group is registered. See docs/adr/007, point 5.
_s5_group = build_system_group("s5", "System 5 — Identity, Values and Policy")


@_s5_group.command(name="log-decision")
@click.argument("tension")
@click.argument("decision")
@click.pass_context
def s5_log_decision(ctx, tension, decision):
    """Append one entry to the homeostat decision log (homeostat/decisions.yaml)."""
    root = paths.homeostat_root(ctx.obj["config"])
    homeostat.record_decision(root, tension, decision)
    click.echo(f"Recorded: {root / 'decisions.yaml'}")


cli.add_command(_s5_group)


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


def _echo_periodic_status(root, tasks):
    """Shared by every periodic pipeline's `status` command (homeostat, ADR
    007; newsletter, ADR 008) — one row per task, straight from the ledger.
    """
    for row in orchestrator.periodic_status(root, tasks):
        label = f"{row['system']}.{row['name']}"
        if row["status"] == "done":
            click.echo(f"  [x] {label:<20} {row.get('completed_at', '')}  {row.get('output', '')}")
        elif row["status"] == "failed":
            click.echo(f"  [!] {label:<20} failed — {row.get('error')}")
        else:
            click.echo(f"  [ ] {label:<20} never run")


def _run_periodic_command(root, config, tasks, step, complete_message):
    """Shared by every periodic pipeline's `run` command — runs the chain via
    `orchestrator.run_periodic()`, echoes the summary, exits non-zero on any
    failure so scripting/cron callers can detect it.
    """
    summary = orchestrator.run_periodic(root, config, tasks, step=step)
    click.echo(f"\nDone: {len(summary['done'])}, Failed: {len(summary['failed'])}")
    for label in summary["done"]:
        click.echo(f"  [x] {label}")
    for label in summary["failed"]:
        click.echo(f"  [!] {label}")
    if summary["complete"]:
        click.echo(complete_message)
    if summary["failed"]:
        sys.exit(1)


@click.group(name="homeostat", help="System 5's homeostat pipeline (not book-scoped) — see ADR 007")
def s2_homeostat():
    pass


@s2_homeostat.command(name="status")
@click.pass_context
def s2_homeostat_status(ctx):
    """Show each homeostat stage's last recorded outcome."""
    root = paths.homeostat_root(ctx.obj["config"])
    click.echo(f"Homeostat ({root})\n")
    _echo_periodic_status(root, orchestrator.HOMEOSTAT_TASKS)


@s2_homeostat.command(name="run")
@click.option("--step", is_flag=True, help="Run exactly one stage and stop.")
@click.pass_context
def s2_homeostat_run(ctx, step):
    """Run homeostat-scan -> homeostat -> homeostat-render, in order, unconditionally."""
    config = ctx.obj["config"]
    root = paths.homeostat_root(config)
    _run_periodic_command(root, config, orchestrator.HOMEOSTAT_TASKS, step, "Homeostat chain complete.")


@click.group(name="newsletter", help="System 1D's monthly newsletter pipeline (not book-scoped) — see ADR 008")
def s2_newsletter():
    pass


@s2_newsletter.command(name="status")
@click.pass_context
def s2_newsletter_status(ctx):
    """Show each newsletter stage's last recorded outcome."""
    root = paths.newsletter_root(ctx.obj["config"])
    click.echo(f"Newsletter ({root})\n")
    _echo_periodic_status(root, orchestrator.NEWSLETTER_TASKS)


@s2_newsletter.command(name="run")
@click.option("--step", is_flag=True, help="Run exactly one stage and stop.")
@click.pass_context
def s2_newsletter_run(ctx, step):
    """Run newsletter-scan -> newsletter -> newsletter-track, in order, unconditionally."""
    config = ctx.obj["config"]
    root = paths.newsletter_root(config)
    _run_periodic_command(root, config, orchestrator.NEWSLETTER_TASKS, step, "Newsletter chain complete.")


s2.add_command(s2_homeostat)
s2.add_command(s2_newsletter)
cli.add_command(s2)


# ---------------------------------------------------------------------------
# System 3 — Performance Monitoring. Hand-written like s2: read-only
# aggregation over the run-state ledger, no tasks of its own. Deliberately
# reporting-only — no resource allocation, budget tracking, or decisions.
# See docs/adr/005-system-3-performance-monitoring.md.
# ---------------------------------------------------------------------------

def _format_duration(seconds: float) -> str:
    seconds = int(round(seconds or 0))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def _format_cost(cost_usd) -> str:
    if cost_usd is None:
        return "-"
    # Per-task costs for short texts are routinely sub-cent — 3 decimals
    # would silently round them to "$0.000", hiding real signal.
    return f"${cost_usd:.6f}" if cost_usd < 0.01 else f"${cost_usd:.3f}"


def _format_span(start_iso: str, end_iso: str) -> str:
    if not start_iso:
        return "no runs yet"
    start = date.fromisoformat(start_iso[:10])
    end = date.fromisoformat(end_iso[:10])
    days = (end - start).days
    label = "in progress" if days == 0 else f"{days} day{'s' if days != 1 else ''}"
    return f"{start} -> {end} ({label})"


@click.group(name="s3", help="System 3 — Performance Monitoring (metrics dashboard)")
def s3():
    pass


@s3.command(name="dashboard")
@click.argument("book_slug", required=False)
@click.pass_context
def s3_dashboard(ctx, book_slug):
    """Show captured cost/duration metrics — one book, or the whole portfolio."""
    config = ctx.obj["config"]

    if book_slug:
        root = _resolve_book_root(config, book_slug)
        summary = dashboard.book_summary(root)
        click.echo(f"Book: {book_slug}\n")
        click.echo(f"{'Task':<24}  {'Duration':>8}  {'Provider/Model':<32}  {'Usage':<22}  {'Cost':>10}")
        for key, entry in sorted(summary["tasks"].items()):
            if entry.get("status") != "done":
                continue
            provider_model = entry.get("model") or entry.get("provider") or "-"
            if entry.get("model") and entry.get("provider"):
                provider_model = f"{entry['provider']} / {entry['model']}"
            usage = entry.get("usage")
            if usage and "total_tokens" in usage:
                usage_str = f"{usage['total_tokens']:,} tokens"
            elif usage and "characters" in usage:
                usage_str = f"{usage['characters']:,} characters"
            else:
                usage_str = "-"
            click.echo(f"{key:<24}  {_format_duration(entry.get('duration_seconds')):>8}  "
                       f"{provider_model:<32}  {usage_str:<22}  {_format_cost(entry.get('cost_usd')):>10}")

        cost_note = "" if summary["cost_usd"] is not None else " (cost omitted — no pricing configured, or no billable tasks run yet)"
        click.echo(f"\nTotal: {summary['tasks_done']}/{summary['tasks_total']} tasks done, "
                   f"{_format_duration(summary['duration_seconds'])} compute time, "
                   f"{_format_cost(summary['cost_usd'])} API cost{cost_note}")
        click.echo(f"In-pipeline span: {_format_span(summary['span_start'], summary['span_end'])}")
        return

    books_dir = Path(config.get("books_dir", "books"))
    summaries = dashboard.portfolio_summary(books_dir)
    if not summaries:
        click.echo(f"No books found under {books_dir}/")
        return

    click.echo(f"Portfolio: {len(summaries)} book{'s' if len(summaries) != 1 else ''}\n")
    click.echo(f"{'Book':<20}  {'Tasks done':<12}  {'API cost':<12}  {'Compute time':<14}  {'In-pipeline span'}")
    for s in summaries:
        done_str = f"{s['tasks_done']}/{s['tasks_total']}"
        click.echo(f"{s['slug']:<20}  {done_str:<12}  {_format_cost(s['cost_usd']):<12}  "
                   f"{_format_duration(s['duration_seconds']):<14}  {_format_span(s['span_start'], s['span_end'])}")

    total_done = sum(s["tasks_done"] for s in summaries)
    known_costs = [s["cost_usd"] for s in summaries if s["cost_usd"] is not None]
    total_cost = sum(known_costs) if known_costs else None
    total_duration = sum(s["duration_seconds"] for s in summaries)
    avg_cost = (total_cost / len(known_costs)) if known_costs else None
    avg_duration = total_duration / len(summaries) if summaries else 0

    click.echo(f"\nTotals: {_format_cost(total_cost)} API cost, {total_done} tasks completed, "
               f"avg {_format_cost(avg_cost)}/title, avg compute time {_format_duration(avg_duration)}/title")


cli.add_command(s3)


# ---------------------------------------------------------------------------
# candidate new — bootstrap for a text under System 5 policy evaluation.
# Pure scaffolding, like `init`: it creates a folder and a manifest, nothing
# more. It does no System 1A work (identifying, sourcing, or assessing a
# candidate) — a human still does all of that; this just makes room for the
# result. See docs/adr/006-system-5-policy-agent.md.
# ---------------------------------------------------------------------------

@click.group(name="candidate", help="Candidates under System 5 policy evaluation")
def candidate():
    pass


@candidate.command(name="new")
@click.argument("candidate_slug")
@click.pass_context
def candidate_new(ctx, candidate_slug):
    """Create the candidates/ folder (if needed) and a place for this candidate's brief."""
    root = paths.candidates_root(ctx.obj["config"])
    briefs_dir = root / "s1a" / "briefs"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    brief_path = briefs_dir / f"{candidate_slug}.txt"

    click.echo(f"Write the candidate's description in: {brief_path}")
    click.echo(f"Then run: pipeline.py s5 evaluate {brief_path}")


cli.add_command(candidate)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()
