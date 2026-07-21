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
import re
import sys
from datetime import date
from pathlib import Path

import click
import yaml
from dotenv import load_dotenv

from lib import candidates, dashboard, homeostat, manifest, orchestrator, paths, policy_check
from lib import task_loader
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
# book — small, explicit commands for per-book facts nobody should mistype
# or that need setting after init (an ISBN is usually assigned well into
# production, not at init time). See docs/adr/011-system-3-sales-ingestion.md,
# point 2 — this is deliberately NOT part of marketing_metadata.yaml (free-
# form, human-facing marketing copy) or a hand-edit to manifest.yaml.
# ---------------------------------------------------------------------------

@click.group(name="book", help="Per-book facts and bootstrap commands")
def book():
    pass


@book.command(name="set-isbn")
@click.argument("book_slug")
@click.argument("isbn")
@click.pass_context
def book_set_isbn(ctx, book_slug, isbn):
    """Record a book's ISBN — the join key System 3's sales-ingest (ADR 011)
    matches royalty report rows against."""
    root = _resolve_book_root(ctx.obj["config"], book_slug)
    manifest.update(root, isbn=isbn)
    click.echo(f"Recorded ISBN {isbn} for {book_slug}")


cli.add_command(book)


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


@_s5_group.command(name="check-policy-sync")
@click.option("--update", "do_update", is_flag=True,
              help="Rewrite the stored sync hash after confirming the prompt was updated by hand.")
def s5_check_policy_sync(do_update):
    """Check prompts/s5/policy_evaluation_task.txt against docs/vsm.md's
    Editorial Policy subsection (ADR 013) — catches a stale prompt after
    vsm.md's policy is revised."""
    current = policy_check.current_hash()
    if do_update:
        policy_check.update_stored_hash(current)
        click.echo(f"Updated stored hash to {current}.")
        return

    stored = policy_check.stored_hash()
    if stored == current:
        click.echo(f"In sync (hash {current}).")
        return

    click.echo(
        f"OUT OF SYNC — docs/vsm.md's Editorial Policy subsection has changed since this prompt was last synced.\n"
        f"  Stored hash:  {stored or '(none recorded)'}\n"
        f"  Current hash: {current}\n"
        f"Review docs/vsm.md's \"### Editorial Policy (Constitutive Criteria)\" subsection against "
        f"prompts/s5/policy_evaluation_task.txt, update the prompt if the policy actually changed, "
        f"then run `pipeline.py s5 check-policy-sync --update`.",
        err=True,
    )
    sys.exit(1)


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
    """Show every book-scoped task's status: done, stale, ready, blocked, or failed."""
    root = _resolve_book_root(ctx.obj["config"], book_slug)
    click.echo(f"Book: {book_slug} ({root})")

    current_system = None
    for row in orchestrator.book_status(root):
        if row["system"] != current_system:
            current_system = row["system"]
            click.echo(f"\n{_SYSTEM_LABELS[current_system]}")

        name = row["name"]
        edited = " (edited since run)" if row.get("edited_since_run") else ""
        if row["status"] == "done":
            click.echo(f"  [x] {name:<20} {row['output']}{edited}")
        elif row["status"] == "stale":
            retry = "ready to rerun" if row["ready_to_rerun"] else "blocked"
            click.echo(f"  [~] {name:<20} stale ({retry}) — invalidated by {row['invalidated_by']}{edited}")
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
# System 3 — Performance Monitoring. `dashboard` is hand-written, read-only
# aggregation over the run-state ledger — deliberately reporting-only, no
# resource allocation, budget tracking, or decisions (ADR 005). Since
# ADR 011, S3 also owns one real, manifest-driven task (`sales-ingest`),
# mixing both shapes in one group — same mixed pattern `s5` already uses
# (ADR 007 point 5), except here it's the *task-declared* command that's
# hand-written (its input is an external download, not a file already
# inside a book's folder — see docs/adr/011-system-3-sales-ingestion.md,
# point 4), and `dashboard` that stays hand-written for the opposite reason
# (it was never task-shaped to begin with).
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


def _format_margin(margin_usd) -> str:
    if margin_usd is None:
        return "-"
    sign = "-" if margin_usd < 0 else ""
    return f"{sign}${abs(margin_usd):.2f}"


def _format_revenue(revenue_by_currency: dict) -> str:
    if not revenue_by_currency:
        return "-"
    # Grouped by currency, not summed across them (ADR 011, point 6) — no
    # FX conversion is attempted anywhere in this project.
    return ", ".join(f"{amount:.2f} {currency}" for currency, amount in sorted(revenue_by_currency.items()))


def _format_span(start_iso: str, end_iso: str) -> str:
    if not start_iso:
        return "no runs yet"
    start = date.fromisoformat(start_iso[:10])
    end = date.fromisoformat(end_iso[:10])
    days = (end - start).days
    label = "in progress" if days == 0 else f"{days} day{'s' if days != 1 else ''}"
    return f"{start} -> {end} ({label})"


_s3_group = build_system_group("s3", "System 3 — Performance Monitoring (metrics dashboard)")


@_s3_group.command(name="dashboard")
@click.argument("book_slug", required=False)
@click.pass_context
def s3_dashboard(ctx, book_slug):
    """Show captured cost/duration metrics — one book, or the whole portfolio."""
    config = ctx.obj["config"]
    books_dir = Path(config.get("books_dir", "books"))

    if book_slug:
        root = _resolve_book_root(config, book_slug)
        summary = dashboard.book_summary(root)
        # Duration outliers are peer-compared across the whole portfolio
        # (ADR 010) — same task name on every other book — so the flags have
        # to be computed against every book's summary, not just this one.
        flags = dashboard.deviation_flags(dashboard.portfolio_summary(books_dir), config)
        duration_flags = {key: vals for (slug, key), vals in flags["duration"].items() if slug == book_slug}

        click.echo(f"Book: {book_slug}\n")
        click.echo(f"{'Task':<24}  {'Duration':>10}  {'Provider/Model':<32}  {'Usage':<22}  {'Cost':>10}")
        for key, entry in sorted(summary["tasks"].items()):
            if entry.get("status") not in ("done", "stale"):
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
            label = key if entry["status"] == "done" else f"{key} (stale)"
            duration_str = _format_duration(entry.get("duration_seconds"))
            if key in duration_flags:
                duration_str += " !"
            click.echo(f"{label:<24}  {duration_str:>10}  "
                       f"{provider_model:<32}  {usage_str:<22}  {_format_cost(entry.get('cost_usd')):>10}")

        cost_note = "" if summary["cost_usd"] is not None else " (cost omitted — no pricing configured, or no billable tasks run yet)"
        click.echo(f"\nTotal: {summary['tasks_done']}/{summary['tasks_total']} tasks done, "
                   f"{_format_duration(summary['duration_seconds'])} compute time, "
                   f"{_format_cost(summary['cost_usd'])} API cost{cost_note}")
        click.echo(f"In-pipeline span: {_format_span(summary['span_start'], summary['span_end'])}")
        if summary["revenue_by_currency"]:
            click.echo(f"Revenue: {_format_revenue(summary['revenue_by_currency'])}")
            click.echo(f"Reported margin (revenue minus tracked API cost only): "
                       f"{_format_margin(summary['reported_margin_usd'])}")
        for key, (value, avg, mult) in sorted(duration_flags.items()):
            click.echo(f"\nFlagged: {key} — {_format_duration(value)} is {mult:.1f}x the portfolio "
                       f"average for this task ({_format_duration(avg)})")
        return

    summaries = dashboard.portfolio_summary(books_dir)
    if not summaries:
        click.echo(f"No books found under {books_dir}/")
        return

    flags = dashboard.deviation_flags(summaries, config)

    click.echo(f"Portfolio: {len(summaries)} book{'s' if len(summaries) != 1 else ''}\n")
    click.echo(f"{'Book':<20}  {'Tasks done':<12}  {'API cost':<14}  {'Compute time':<14}  "
               f"{'Revenue':<16}  {'Margin (API)':<12}  {'In-pipeline span'}")
    for s in summaries:
        done_str = f"{s['tasks_done']}/{s['tasks_total']}"
        cost_str = _format_cost(s["cost_usd"])
        if s["slug"] in flags["cost"]:
            cost_str += " !"
        click.echo(f"{s['slug']:<20}  {done_str:<12}  {cost_str:<14}  "
                   f"{_format_duration(s['duration_seconds']):<14}  "
                   f"{_format_revenue(s['revenue_by_currency']):<16}  "
                   f"{_format_margin(s['reported_margin_usd']):<12}  "
                   f"{_format_span(s['span_start'], s['span_end'])}")

    total_done = sum(s["tasks_done"] for s in summaries)
    known_costs = [s["cost_usd"] for s in summaries if s["cost_usd"] is not None]
    total_cost = sum(known_costs) if known_costs else None
    total_duration = sum(s["duration_seconds"] for s in summaries)
    avg_cost = (total_cost / len(known_costs)) if known_costs else None
    avg_duration = total_duration / len(summaries) if summaries else 0

    click.echo(f"\nTotals: {_format_cost(total_cost)} API cost, {total_done} tasks completed, "
               f"avg {_format_cost(avg_cost)}/title, avg compute time {_format_duration(avg_duration)}/title")

    for slug, (value, avg, mult) in sorted(flags["cost"].items()):
        click.echo(f"\nFlagged: {slug} — API cost {_format_cost(value)} is {mult:.1f}x "
                   f"the portfolio average ({_format_cost(avg)})")


# Deliberately re-registers "sales-ingest" on `_s3_group`, overriding the
# single-positional-file command `build_system_group()` already generated
# for it from systems/s3/tasks.yaml (Click's Group.add_command() just
# overwrites the dict entry — later registration wins, no error). That
# auto-generated version would resolve root by walking up from the CSV's
# own path (paths.book_root()), which can never work here: the CSV is an
# external download, not a file inside any book folder. See
# docs/adr/011-system-3-sales-ingestion.md, point 4.
@_s3_group.command(name="sales-ingest")
@click.argument("book_slug")
@click.argument("csv_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--format", "format_", default=None,
              help="Force a specific platform format instead of auto-detecting from the CSV's header row.")
@click.pass_context
def s3_sales_ingest(ctx, book_slug, csv_file, format_):
    """Ingest a manually-downloaded royalty/sales CSV for one book.

    Unlike every other task's command, this one names the book explicitly
    (book_slug first) rather than resolving it by walking up from the input
    file — the CSV is an external download with no book folder anywhere in
    its ancestry.
    """
    config = ctx.obj["config"]
    root = _resolve_book_root(config, book_slug)
    task = next(t for t in task_loader.load_system_tasks("s3") if t["name"] == "sales-ingest")
    task_loader.run_task(root, config, "s3", task, input_file=Path(csv_file).resolve(), format=format_)


cli.add_command(_s3_group)


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


@candidate.command(name="record-decision")
@click.argument("candidate_slug")
@click.argument("decision")
@click.pass_context
def candidate_record_decision(ctx, candidate_slug, decision):
    """Record what the Director actually decided, against s5 evaluate's own
    verdict, for calibration (candidates/calibration.yaml). DECISION is free
    text — e.g. acquired, declined, deferred."""
    root = paths.candidates_root(ctx.obj["config"])
    verdict_path = root / "s5" / "evaluate" / f"{candidate_slug}.txt"
    if not verdict_path.exists():
        raise click.ClickException(
            f"No verdict found at {verdict_path} — run `s5 evaluate` for this candidate first."
        )
    match = re.search(r"<verdict>\s*(.*?)\s*</verdict>", verdict_path.read_text(encoding="utf-8"))
    agent_verdict = match.group(1) if match else "(sin etiqueta <verdict>)"
    candidates.record_calibration(root, candidate_slug, agent_verdict, decision)
    click.echo(f"Recorded: {root / 'calibration.yaml'} ({agent_verdict} -> {decision})")


cli.add_command(candidate)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cli()
