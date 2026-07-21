from pathlib import Path
import yaml


def load(book_dir: Path) -> dict:
    path = book_dir / "manifest.yaml"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def save(book_dir: Path, data: dict) -> None:
    path = book_dir / "manifest.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)


def update(book_dir: Path, **kwargs) -> None:
    if "tasks" in kwargs:
        raise ValueError(
            "'tasks' is the run-state ledger (ADR 004/009) — write it via "
            "record_task()/mark_stale(), never update(); an engine writing "
            "it directly would silently desync s2's view of reality."
        )
    if "sales" in kwargs:
        raise ValueError(
            "'sales' is an append-only, deduped ledger (ADR 011) — write it "
            "via record_sale(), never update(); a blind overwrite would lose "
            "every previously-ingested reporting period."
        )
    data = load(book_dir)
    data.update(kwargs)
    save(book_dir, data)


def record_task(book_dir: Path, key: str, **fields) -> None:
    """Write one entry into the run-state ledger (manifest.yaml's `tasks:`
    block), keyed as "<system>.<task-name>" — see ADR 004. A targeted merge:
    only `data["tasks"][key]` is touched, so concurrent facts recorded via
    `update()` (the legacy flat keys) and unrelated ledger entries are left
    untouched.
    """
    data = load(book_dir)
    tasks = data.setdefault("tasks", {})
    tasks[key] = fields
    save(book_dir, data)


def mark_stale(book_dir: Path, key: str, invalidated_by: str) -> None:
    """Flip a `done` ledger entry to `stale` — used by System 2's invalidation
    cascade (ADR 009) when an upstream task it depends on re-runs. A no-op if
    the entry isn't currently `done`: already-`stale`, `failed`, or never-run
    entries are left exactly as they are (see `orchestrator.cascade_invalidate()`).
    Every other field is preserved from the last real completion — a `stale`
    entry is downgraded, not erased.
    """
    data = load(book_dir)
    tasks = data.setdefault("tasks", {})
    entry = tasks.get(key)
    if entry and entry.get("status") == "done":
        tasks[key] = {**entry, "status": "stale", "invalidated_by": invalidated_by}
        save(book_dir, data)


def record_sale(book_dir: Path, entries: list) -> tuple:
    """Append new sales entries to manifest.yaml's `sales:` block — deduped
    on `(platform, period_start, period_end)` within this one book's own
    file (`isbn` is implicit: every entry passed in here has already been
    filtered to this book's own ISBN by the caller, so it isn't re-stored
    per entry) — so re-ingesting the same export twice is a no-op, not
    double-counted revenue. See ADR 011. Returns (added_count, skipped_count).
    """
    data = load(book_dir)
    sales = data.setdefault("sales", [])
    existing = {(e.get("platform"), e.get("period_start"), e.get("period_end")) for e in sales}
    added = 0
    for entry in entries:
        key = (entry.get("platform"), entry.get("period_start"), entry.get("period_end"))
        if key in existing:
            continue
        sales.append(entry)
        existing.add(key)
        added += 1
    if added:
        save(book_dir, data)
    return added, len(entries) - added
