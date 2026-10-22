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
