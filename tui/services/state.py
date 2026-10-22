"""Reading pipeline state for the screens (ADR 023, Decision 2).

Thin wrappers over `lib/` read functions (`orchestrator.book_status`,
`dashboard`, `manifest.load`, ...): no pipeline logic is re-implemented here, so a
changed signature in `lib/` breaks a test immediately. Nothing in this module
writes anything. Plain Python, no Textual.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from lib import manifest, orchestrator
from tui.services.context import Context


@dataclass
class BookRow:
    slug: str
    path: Path
    tasks_total: int
    done: int
    failed: int
    stale: int
    isbn: str = None
    last_activity: str = None     # ISO timestamp, UTC


def _last_activity(root: Path, ledger: dict) -> str:
    stamps = [s for entry in ledger.values() for s in (entry.get("completed_at"), entry.get("attempted_at")) if s]
    if stamps:
        return max(stamps)
    mtime = (root / "manifest.yaml").stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(timespec="seconds")


def book_row(root: Path) -> BookRow:
    data = manifest.load(root)
    rows = orchestrator.book_status(root)
    counts = {status: sum(1 for r in rows if r["status"] == status) for status in ("done", "failed", "stale")}
    return BookRow(
        slug=root.name, path=root, tasks_total=len(rows), done=counts["done"], failed=counts["failed"],
        stale=counts["stale"], isbn=data.get("isbn") or None,
        last_activity=_last_activity(root, data.get("tasks", {})),
    )


def list_books(ctx: Context) -> list:
    """Every book under `books_dir`, most recently active first."""
    books_dir = ctx.data_dir("books_dir")
    if not books_dir.is_dir():
        return []
    rows = [book_row(p) for p in sorted(books_dir.iterdir()) if p.is_dir() and (p / "manifest.yaml").is_file()]
    return sorted(rows, key=lambda r: r.last_activity or "", reverse=True)


def format_activity(iso: str) -> str:
    return iso[:16].replace("T", " ") if iso else "-"


# -- one book's tasks (T2.2) ---------------------------------------------------

@dataclass
class TaskRow:
    system: str
    name: str
    status: str                 # done | stale | ready | blocked | failed
    detail: str                 # one line: the output, the reason, the error ...
    output: str = None          # the ledger's output path, relative to the book
    edited: bool = False        # output edited by hand since the task ran (informational)
    runnable: bool = False      # can be started now (re-running a done task is allowed, with a warning)
    invalidated_by: str = None

    @property
    def key(self) -> str:
        return f"{self.system}.{self.name}"


def describe_task(row: dict) -> TaskRow:
    """One `orchestrator.book_status` row as the screen shows it. The words match `s2 status`."""
    status = row["status"]
    edited = " (edited since run)" if row.get("edited_since_run") else ""
    common = dict(system=row["system"], name=row["name"], status=status, edited=bool(row.get("edited_since_run")))
    if status == "done":
        return TaskRow(detail=f"{row['output']}{edited}", output=row["output"], runnable=True, **common)
    if status == "stale":
        ready = row["ready_to_rerun"]
        return TaskRow(detail=f"invalidated by {row['invalidated_by']} - {'ready to rerun' if ready else 'blocked'}{edited}",
                       output=row.get("output"), runnable=ready, invalidated_by=row["invalidated_by"], **common)
    if status == "failed":
        ready = row["ready_to_retry"]
        return TaskRow(detail=f"{row['error']} - {'ready to retry' if ready else 'blocked'}", runnable=ready, **common)
    if status == "ready":
        return TaskRow(detail="ready", runnable=True, **common)
    return TaskRow(detail=row["reason"], runnable=False, **common)


def book_tasks(root: Path) -> list:
    return [describe_task(r) for r in orchestrator.book_status(root)]


def task_input(root: Path, row: TaskRow):
    """(input path, reason): the input `s2 run` would give this task (see `orchestrator.resolve_input`)."""
    return orchestrator.resolve_input(root, row.key)


def stale_consequences(root: Path, row: TaskRow) -> list:
    """Task keys that are done now and would be marked stale if `row` ran again (ADR 009)."""
    ledger = manifest.load(root).get("tasks", {})
    return [key for key in orchestrator.downstream_of(row.key) if ledger.get(key, {}).get("status") == "done"]


def ready_task_keys(root: Path, only: str = None) -> list:
    """Keys of the tasks `s2 run` would start right now (failed ones included: they are retried)."""
    return [f"{node['system']}.{node['task']['name']}" for node, _ in orchestrator.ready_tasks(root, only=only)]
