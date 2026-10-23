"""Books as a whole: finished or not, and how a list of them is ordered.

A finished book carries `done_on: <YYYY-MM-DD>` at the top level of its `manifest.yaml`;
a book without that key is still in progress. It is a label for the people reading the
lists, nothing more: the run-state ledger, `s2 run`, the dashboard and the sales figures
treat a finished book exactly like any other, so nothing is hidden or lost and a book
can be reopened. (Not to be confused with a task's `status: done` in the ledger.)
"""
from datetime import date, datetime, timezone
from pathlib import Path

from lib import manifest

DONE_KEY = "done_on"


def done_on(root: Path):
    """The date the book was marked done (ISO string), or None if it is still in progress."""
    return manifest.load(root).get(DONE_KEY) or None


def mark_done(root: Path, on: date = None) -> tuple:
    """Mark a book done. Returns (changed, date): `changed` is False, and nothing is written,
    when the book was already marked (its original date is kept)."""
    existing = done_on(root)
    if existing:
        return False, str(existing)
    when = (on or date.today()).isoformat()
    manifest.update(root, **{DONE_KEY: when})
    return True, when


def reopen(root: Path) -> bool:
    """Take the done mark off. Returns False if the book was not marked."""
    data = manifest.load(root)
    if DONE_KEY not in data:
        return False
    del data[DONE_KEY]
    manifest.save(root, data)
    return True


def last_activity(root: Path, ledger: dict = None) -> str:
    """ISO timestamp of the latest task attempt or completion in the ledger; for a book that has never
    run, when its manifest was last written."""
    ledger = manifest.load(root).get("tasks", {}) if ledger is None else ledger
    stamps = [s for entry in ledger.values() for s in (entry.get("completed_at"), entry.get("attempted_at")) if s]
    if stamps:
        return max(stamps)
    mtime = (root / "manifest.yaml").stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(timespec="seconds")


def order(items: list, is_done, activity) -> list:
    """Books in progress first, most recently active first; finished books after them, in the same order."""
    by_activity = sorted(items, key=lambda item: activity(item) or "", reverse=True)
    return sorted(by_activity, key=lambda item: bool(is_done(item)))   # a stable sort: False before True
