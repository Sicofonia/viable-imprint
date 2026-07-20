"""System 2 — book run-state and task orchestration. See ADR 004.

Builds the cross-system task graph from `s1b`/`s1d`'s `tasks.yaml` manifests
(each task's `input:` field, or positional chaining within a system when
`input:` is absent), and computes status/readiness against a book's
`manifest.yaml` run-state ledger (`lib.manifest.record_task`).

Scoped to book-scoped systems only — System 4 isn't book-scoped (ADR 003)
and its two-task chain isn't the coordination problem this solves.

This file has two genuinely different execution models side by side, not
one generalized to cover both:
- `run_book()` / `_load_graph()`: readiness-gated, "done means done forever,"
  fan-out aware — book production.
- `run_homeostat()` (ADR 007) and any future periodic pipeline: unconditional,
  strictly linear re-execution every call — there is no "already done" check,
  because a monthly artifact is meant to be redone every period, not skipped.
Don't assume these are interchangeable.
"""
import importlib
from pathlib import Path

import click

from lib import manifest, task_loader

BOOK_SYSTEMS = ("s1b", "s1d")

# System 5's homeostat pipeline (ADR 007) — explicit, ordered (system, task
# name) list rather than "every task in system s5", because s5's tasks.yaml
# spans two different roots (`evaluate` -> candidates/, this trio ->
# homeostat/), unlike s1b/s1d (uniformly book-scoped) or s4 (uniformly
# intelligence/-scoped).
HOMEOSTAT_TASKS = [("s5", "homeostat-scan"), ("s5", "homeostat"), ("s5", "homeostat-render")]

# System 1D's monthly newsletter pipeline (ADR 008) — same periodic shape as
# HOMEOSTAT_TASKS. Unlike homeostat (a whole system, s5, that's simply never
# in BOOK_SYSTEMS), these three tasks are declared inside s1d's own
# tasks.yaml alongside book-scoped tasks — see the `book_scoped` filter in
# `_load_graph()` below, which is what keeps them out of book production's
# task graph.
NEWSLETTER_TASKS = [("s1d", "newsletter-scan"), ("s1d", "newsletter"), ("s1d", "newsletter-track")]


def _load_graph() -> list:
    """Ordered list of {system, task, depends_on} across every book-scoped
    system. `depends_on` is a ledger key ("<system>.<task-name>"), or None
    for a system's first task when it declares no explicit `input:`.

    Excludes any task declaring `book_scoped: false` (ADR 008) — a periodic,
    non-book task (e.g. System 1D's newsletter trio) can live in the same
    `tasks.yaml` as book-scoped tasks without being pulled into every book's
    graph.
    """
    graph = []
    for system in BOOK_SYSTEMS:
        tasks = [t for t in task_loader.load_system_tasks(system) if t.get("book_scoped", True)]
        for i, task in enumerate(tasks):
            depends_on = task.get("input")
            if depends_on is None and i > 0:
                depends_on = f"{system}.{tasks[i - 1]['name']}"
            graph.append({"system": system, "task": task, "depends_on": depends_on})
    return graph


def _source_file(root: Path):
    """The one file `s1b`'s first task reads, discovered by convention
    (`init` creates `s1b/source/`) rather than a ledger lookup — there is no
    predecessor task to depend on.
    """
    source_dir = root / "s1b" / "source"
    if not source_dir.is_dir():
        return None, "no s1b/source/ folder"
    files = [f for f in source_dir.iterdir() if f.is_file()]
    if len(files) == 0:
        return None, "no file in s1b/source/"
    if len(files) > 1:
        return None, f"{len(files)} files in s1b/source/ — expected exactly one"
    return files[0], None


def book_status(root: Path) -> list:
    """One row per task in the graph: done (with output), ready, blocked (with
    reason), or failed (with the recorded error; still eligible for retry).
    """
    ledger = manifest.load(root).get("tasks", {})
    rows = []
    for node in _load_graph():
        system, task, depends_on = node["system"], node["task"], node["depends_on"]
        name = task["name"]
        key = f"{system}.{name}"
        entry = ledger.get(key)

        if entry and entry.get("status") == "done":
            rows.append({"system": system, "name": name, "status": "done", "output": entry["output"]})
            continue

        if depends_on is None:
            _, reason = _source_file(root)
        else:
            dep_entry = ledger.get(depends_on)
            reason = None if (dep_entry and dep_entry.get("status") == "done") else f"waiting on {depends_on}"
        ready = reason is None

        if entry and entry.get("status") == "failed":
            rows.append({"system": system, "name": name, "status": "failed",
                         "error": entry.get("error"), "ready_to_retry": ready})
        elif ready:
            rows.append({"system": system, "name": name, "status": "ready"})
        else:
            rows.append({"system": system, "name": name, "status": "blocked", "reason": reason})
    return rows


def ready_tasks(root: Path, only: str = None) -> list:
    """Every graph node not yet `done` whose dependency is satisfied —
    including a previously `failed` task, which is always eligible for retry.
    Returns (node, resolved_input_path) pairs.
    """
    ledger = manifest.load(root).get("tasks", {})
    ready = []
    for node in _load_graph():
        system, task, depends_on = node["system"], node["task"], node["depends_on"]
        if only and system != only:
            continue
        key = f"{system}.{task['name']}"
        entry = ledger.get(key)
        if entry and entry.get("status") == "done":
            continue

        if depends_on is None:
            source, _ = _source_file(root)
            if source is not None:
                ready.append((node, source))
        else:
            dep_entry = ledger.get(depends_on)
            if dep_entry and dep_entry.get("status") == "done":
                ready.append((node, root / dep_entry["output"]))
    return ready


def run_book(root: Path, config: dict, only: str = None, step: bool = False) -> dict:
    """Run every currently-ready task, recomputing readiness after each one,
    until nothing further is unblocked. Independent siblings (e.g. System 1D's
    fan-out tasks, which all depend only on `s1d.brief`) keep running even if
    one fails — a chained failure naturally blocks whatever depends on it,
    since nothing downstream ever sees a non-`done` ledger entry.

    Each task is attempted at most once per call, success or failure — a
    `failed` task is retried on the *next* `run_book` call, not looped on
    within this one (its ledger entry stays non-`done`, so it would otherwise
    reappear in every readiness pass forever).
    """
    done, failed = [], []
    attempted = set()
    while True:
        batch = [(node, input_path) for node, input_path in ready_tasks(root, only=only)
                 if f"{node['system']}.{node['task']['name']}" not in attempted]
        if not batch:
            break
        for node, input_path in batch:
            system, task = node["system"], node["task"]
            label = f"{system}.{task['name']}"
            attempted.add(label)
            click.echo(f"[s2] Running {label}...")
            try:
                task_loader.run_task(root, config, system, task, input_file=input_path)
                done.append(label)
            except Exception as e:
                click.echo(f"[s2] {label} failed: {e}")
                failed.append(label)
            if step:
                return _summarize(root, done, failed)
    return _summarize(root, done, failed)


def _summarize(root: Path, done: list, failed: list) -> dict:
    remaining = [r for r in book_status(root) if r["status"] in ("ready", "blocked", "failed")]
    return {"done": done, "failed": failed, "complete": not remaining}


def _task_dict(system: str, name: str) -> dict:
    for task in task_loader.load_system_tasks(system):
        if task["name"] == name:
            return task
    raise ValueError(f"Task {system}.{name} not found in systems/{system}/tasks.yaml")


def run_homeostat(root: Path, config: dict, step: bool = False) -> dict:
    """Run System 5's homeostat chain in declared order, unconditionally —
    periodic, not one-and-done, so there is no readiness/"already done" check
    here at all, unlike `run_book()`. A failure stops the chain immediately:
    this is strictly linear (no independent siblings the way System 1D's
    fan-out tasks are), so a broken `homeostat-scan` must not let `homeostat`
    run against stale or missing input.
    """
    done, failed = [], []
    input_file = None
    for system, name in HOMEOSTAT_TASKS:
        task = _task_dict(system, name)
        engine = importlib.import_module(f"engines.{task['engine']}")
        label = f"{system}.{name}"
        click.echo(f"[s2] Running {label}...")
        try:
            input_file = task_loader.run_task(
                root, config, system, task,
                input_file=input_file if getattr(engine, "CLI_ARG", "file") != "none" else None,
            )
            done.append(label)
        except Exception as e:
            click.echo(f"[s2] {label} failed: {e}")
            failed.append(label)
            break  # linear chain — no point running the next stage against a failure
        if step:
            break
    return {"done": done, "failed": failed, "complete": len(done) == len(HOMEOSTAT_TASKS)}


def homeostat_status(root: Path) -> list:
    """Read-only: each homeostat task's last recorded outcome, straight from
    the ledger — no readiness computation, since there's no notion of
    "blocked" in a chain that's always re-run top to bottom.
    """
    ledger = manifest.load(root).get("tasks", {})
    return [{"system": s, "name": n, **ledger.get(f"{s}.{n}", {"status": "never run"})}
            for s, n in HOMEOSTAT_TASKS]


def run_newsletter(root: Path, config: dict, step: bool = False) -> dict:
    """Run System 1D's newsletter chain in declared order, unconditionally —
    structurally identical to `run_homeostat()` (ADR 007). Deliberately not
    unified into one shared helper yet — see docs/adr/008-system1d-newsletter.md,
    Alternatives: both periodic pipelines were unimplemented ADRs at the same
    time, and coupling their implementations together would have added
    review friction for a small amount of duplicated code. Worth unifying now
    that both exist for real.
    """
    done, failed = [], []
    input_file = None
    for system, name in NEWSLETTER_TASKS:
        task = _task_dict(system, name)
        engine = importlib.import_module(f"engines.{task['engine']}")
        label = f"{system}.{name}"
        click.echo(f"[s2] Running {label}...")
        try:
            input_file = task_loader.run_task(
                root, config, system, task,
                input_file=input_file if getattr(engine, "CLI_ARG", "file") != "none" else None,
            )
            done.append(label)
        except Exception as e:
            click.echo(f"[s2] {label} failed: {e}")
            failed.append(label)
            break
        if step:
            break
    return {"done": done, "failed": failed, "complete": len(done) == len(NEWSLETTER_TASKS)}


def newsletter_status(root: Path) -> list:
    """Read-only: each newsletter task's last recorded outcome. See
    `homeostat_status()`.
    """
    ledger = manifest.load(root).get("tasks", {})
    return [{"system": s, "name": n, **ledger.get(f"{s}.{n}", {"status": "never run"})}
            for s, n in NEWSLETTER_TASKS]
