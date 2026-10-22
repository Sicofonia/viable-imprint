"""Reading pipeline state for the screens (ADR 023, Decision 2).

Thin wrappers over `lib/` read functions (`orchestrator.book_status`,
`dashboard`, `manifest.load`, ...): no pipeline logic is re-implemented here, so a
changed signature in `lib/` breaks a test immediately. Nothing in this module
writes anything. Plain Python, no Textual.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from lib import homeostat, manifest, orchestrator
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


# -- periodic pipelines: homeostat and newsletter (T6.1, T6.2) ---------------------------

@dataclass
class StageRow:
    system: str
    name: str
    status: str          # done | failed | never run
    detail: str          # "<completed_at>  <output>", the error, or ""

    @property
    def key(self) -> str:
        return f"{self.system}.{self.name}"


def periodic_rows(root: Path, tasks: list) -> list:
    """Each stage's last recorded outcome (`orchestrator.periodic_status`), worded like
    `s2 homeostat status` / `s2 newsletter status`."""
    rows = []
    for row in orchestrator.periodic_status(root, tasks):
        status = row["status"]
        if status == "done":
            detail = f"{row.get('completed_at', '')}  {row.get('output', '')}"
        elif status == "failed":
            detail = str(row.get("error"))
        else:
            detail = ""
        rows.append(StageRow(row["system"], row["name"], status, detail))
    return rows


def recorded_output(root: Path, task: tuple):
    """The file a finished periodic stage wrote, if the ledger records one and it still exists."""
    entry = manifest.load(root).get("tasks", {}).get(f"{task[0]}.{task[1]}", {})
    if entry.get("status") != "done" or not entry.get("output"):
        return None
    path = root / entry["output"]
    return path if path.is_file() else None


rendered_output = recorded_output   # the homeostat page and the final newsletter are both "a stage's recorded output"


def latest_briefing_date(ctx: Context):
    """Date folder of the System 4 briefing the homeostat will read, or None (`homeostat.latest_s4_briefing`)."""
    return homeostat.latest_s4_briefing(ctx.config)[1]


def decisions(root: Path) -> list:
    """The homeostat decision log, newest first."""
    logged = homeostat.load_decisions(root)
    return sorted(reversed(logged), key=lambda d: d.get("date", ""), reverse=True)


def featured(root: Path, kind: str) -> list:
    """Explorers or dishes already featured (`featured_<kind>.yaml`, kept by `newsletter-track`),
    newest first. A missing or unreadable file is an empty list, as `newsletter-scan` treats it."""
    path = root / f"featured_{kind}.yaml"
    if not path.is_file():
        return []
    try:
        entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except yaml.YAMLError:
        return []
    return list(reversed(entries)) if isinstance(entries, list) else []


# -- candidates under System 5 evaluation (T7.1) -----------------------------------------------
# Folder conventions from ADR 006: briefs in `candidates/s1a/briefs/<slug>.txt`, verdicts in
# `candidates/s5/evaluate/<slug>.txt`, the Director's decisions in `candidates/calibration.yaml`.

_VERDICT = re.compile(r"<verdict>\s*(.*?)\s*</verdict>")   # the same pattern `candidate record-decision` uses
NO_VERDICT_TAG = "?"


@dataclass
class CandidateRow:
    slug: str
    brief: Path
    verdict_file: Path = None
    state: str = "not evaluated"      # not evaluated | evaluated | decided
    verdict: str = None               # the <verdict> value, "?" when the file has no tag
    decision: str = None
    decided_on: str = None
    brief_empty: bool = False


@dataclass
class CalibrationCount:
    verdict: str
    decision: str
    count: int


def read_verdict(path: Path) -> str:
    match = _VERDICT.search(path.read_text(encoding="utf-8"))
    return match.group(1) if match else NO_VERDICT_TAG


def calibration_entries(root: Path) -> list:
    path = root / "calibration.yaml"
    if not path.is_file():
        return []
    try:
        entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except yaml.YAMLError:
        return []
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def candidate_rows(root: Path) -> list:
    """Every brief under `s1a/briefs/`, by name, with where it stands. The decision shown is the
    latest one logged for the candidate."""
    briefs = root / "s1a" / "briefs"
    if not briefs.is_dir():
        return []
    latest = {}
    for entry in calibration_entries(root):
        latest[entry.get("candidate")] = entry
    rows = []
    for brief in sorted(briefs.glob("*.txt")):
        slug = brief.stem
        row = CandidateRow(slug=slug, brief=brief, brief_empty=not brief.read_text(encoding="utf-8").strip())
        verdict_file = root / "s5" / "evaluate" / f"{slug}.txt"
        if verdict_file.is_file():
            row.verdict_file, row.verdict, row.state = verdict_file, read_verdict(verdict_file), "evaluated"
        if slug in latest:
            row.state = "decided"
            row.decision = str(latest[slug].get("human_decision"))
            row.decided_on = str(latest[slug].get("date", ""))
            if row.verdict is None:
                row.verdict = str(latest[slug].get("agent_verdict"))
        rows.append(row)
    return rows


def calibration_counts(root: Path) -> list:
    """How often each verdict ended in each decision, over everything logged in calibration.yaml."""
    counts = {}
    for entry in calibration_entries(root):
        key = (str(entry.get("agent_verdict")), str(entry.get("human_decision")))
        counts[key] = counts.get(key, 0) + 1
    return [CalibrationCount(v, d, n) for (v, d), n in sorted(counts.items())]
