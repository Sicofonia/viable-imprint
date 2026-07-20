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
