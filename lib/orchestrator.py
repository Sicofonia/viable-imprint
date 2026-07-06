"""System 2 — book run-state and task orchestration. See ADR 004.

Builds the cross-system task graph from `s1b`/`s1d`'s `tasks.yaml` manifests
(each task's `input:` field, or positional chaining within a system when
`input:` is absent), and computes status/readiness against a book's
`manifest.yaml` run-state ledger (`lib.manifest.record_task`).

Scoped to book-scoped systems only — System 4 isn't book-scoped (ADR 003)
and its two-task chain isn't the coordination problem this solves.
"""
from pathlib import Path

import click

from lib import manifest, task_loader

BOOK_SYSTEMS = ("s1b", "s1d")


def _load_graph() -> list:
    """Ordered list of {system, task, depends_on} across every book-scoped
    system. `depends_on` is a ledger key ("<system>.<task-name>"), or None
    for a system's first task when it declares no explicit `input:`.
    """
    graph = []
    for system in BOOK_SYSTEMS:
        tasks = task_loader.load_system_tasks(system)
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
