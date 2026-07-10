"""System 3 — performance-monitoring dashboard. See ADR 005.

Read-only aggregation over each book's run-state ledger (`manifest.yaml`'s
`tasks:` block, ADR 004/005). Scoped to book-scoped systems only (s1b/s1d),
matching `vsm.md`'s System 3 being about title economics, not System 4's
intelligence-scanning cost — see ADR 005, point 8.

Deliberately reporting-only: no resource allocation, budget tracking, or
decision-making. Those stay human, per `vsm.md`'s own split of System 3's
five responsibilities.
"""
from pathlib import Path

from lib import manifest, task_loader
from lib.orchestrator import BOOK_SYSTEMS


def _book_scoped_ledger(root: Path) -> dict:
    tasks = manifest.load(root).get("tasks", {})
    return {k: v for k, v in tasks.items() if k.split(".", 1)[0] in BOOK_SYSTEMS}


def _total_task_count() -> int:
    return sum(len(task_loader.load_system_tasks(s)) for s in BOOK_SYSTEMS)


def book_summary(root: Path) -> dict:
    """One book's aggregated metrics: tasks done/total, total compute time,
    total API cost (None if no task in this book has a known cost — either
    nothing's run yet, or no pricing is configured), and the in-pipeline span
    (earliest to latest `completed_at`) — a proxy for cycle time, not the
    full acquisition-to-shelf number `vsm.md` describes (see ADR 005, point 7).
    """
    ledger = _book_scoped_ledger(root)
    done = [e for e in ledger.values() if e.get("status") == "done"]

    known_costs = [e["cost_usd"] for e in done if e.get("cost_usd") is not None]
    timestamps = sorted(e["completed_at"] for e in done if e.get("completed_at"))

    return {
        "slug": root.name,
        "tasks_done": len(done),
        "tasks_total": _total_task_count(),
        "duration_seconds": round(sum(e.get("duration_seconds", 0) or 0 for e in done), 2),
        "cost_usd": round(sum(known_costs), 6) if known_costs else None,
        "span_start": timestamps[0] if timestamps else None,
        "span_end": timestamps[-1] if timestamps else None,
        "tasks": ledger,
    }


def portfolio_summary(books_dir: Path) -> list:
    books_dir = Path(books_dir)
    if not books_dir.is_dir():
        return []
    return [
        book_summary(book_root)
        for book_root in sorted(books_dir.iterdir())
        if book_root.is_dir() and (book_root / "manifest.yaml").exists()
    ]
