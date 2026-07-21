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
    """Book-scoped task count per system, same `book_scoped` filter
    `orchestrator._load_graph()` applies (ADR 008) — without it, a
    system whose `tasks.yaml` also declares periodic, non-book tasks
    (e.g. S1D's `book_scoped: false` newsletter trio) would inflate
    every book's total by tasks that never actually run against it.
    """
    return sum(
        len([t for t in task_loader.load_system_tasks(s) if t.get("book_scoped", True)])
        for s in BOOK_SYSTEMS
    )


def _revenue_by_currency(root: Path) -> dict:
    """Sum of manifest.yaml's `sales:` entries (ADR 011), grouped by
    currency — deliberately not blended into one number: no FX conversion
    is attempted (see ADR 011, point 6), so a book selling in more than one
    currency shows more than one figure rather than one falsely-precise total.
    """
    sales = manifest.load(root).get("sales", [])
    totals = {}
    for entry in sales:
        currency = entry.get("currency", "USD")
        totals[currency] = round(totals.get(currency, 0.0) + (entry.get("revenue") or 0), 2)
    return totals


def book_summary(root: Path) -> dict:
    """One book's aggregated metrics: tasks done/total, total compute time,
    total API cost (None if no task in this book has a known cost — either
    nothing's run yet, or no pricing is configured), the in-pipeline span
    (earliest to latest `completed_at`) — a proxy for cycle time, not the
    full acquisition-to-shelf number `vsm.md` describes (see ADR 005, point 7)
    — and, since ADR 011, revenue-by-currency and a *reported* margin.

    Counts `stale` (ADR 009) alongside `done`: S2's `stale` means "no longer
    trusted as current," which matters for readiness, not for S3's job here —
    the compute time and API cost were genuinely incurred either way, and
    excluding them the moment an upstream task re-runs would make a book's
    reported spend silently drop.

    `reported_margin_usd` is USD revenue minus tracked API cost ONLY — not
    `vsm.md`'s full gross-margin-per-title metric (design, freelance
    editorial time, ISBN fees, and print cost are not tracked anywhere in
    this project). `None` if there's no USD revenue on record, even if
    there's revenue in another currency — netting a different currency's
    revenue against a USD cost figure would be a real number that looks
    precise and isn't (ADR 011, point 6).
    """
    ledger = _book_scoped_ledger(root)
    completed = [e for e in ledger.values() if e.get("status") in ("done", "stale")]

    known_costs = [e["cost_usd"] for e in completed if e.get("cost_usd") is not None]
    timestamps = sorted(e["completed_at"] for e in completed if e.get("completed_at"))
    cost_usd = round(sum(known_costs), 6) if known_costs else None

    revenue_by_currency = _revenue_by_currency(root)
    reported_margin_usd = None
    if "USD" in revenue_by_currency and cost_usd is not None:
        reported_margin_usd = round(revenue_by_currency["USD"] - cost_usd, 2)

    return {
        "slug": root.name,
        "tasks_done": len(completed),
        "tasks_total": _total_task_count(),
        "duration_seconds": round(sum(e.get("duration_seconds", 0) or 0 for e in completed), 2),
        "cost_usd": cost_usd,
        "span_start": timestamps[0] if timestamps else None,
        "span_end": timestamps[-1] if timestamps else None,
        "tasks": ledger,
        "revenue_by_currency": revenue_by_currency,
        "reported_margin_usd": reported_margin_usd,
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


_MIN_SAMPLE = 3  # below this, a peer average is too noisy to flag against — see ADR 010 point 4


def _peer_average(values: dict, exclude: str) -> float:
    """Mean of every value except `exclude`'s own — deliberately NOT the
    portfolio average including the candidate itself. With self-inclusion
    and a sample right at `_MIN_SAMPLE` (3) compared at the default 3x
    multiplier, no value can *ever* cross the threshold: flagging c requires
    c > k*(a+b+c)/n, i.e. c*(n-k) > k*(a+b), which is unsatisfiable for any
    positive a/b once n == k (found by testing against real numbers during
    implementation — see ADR 010's Implementation notes). Excluding the
    candidate from its own average removes that impossibility outright.
    """
    peers = [v for s, v in values.items() if s != exclude]
    return sum(peers) / len(peers) if peers else 0.0


def _task_duration_baseline(summaries: list) -> dict:
    """task_key -> {slug: duration} for every book that's completed that task
    (done/stale, ADR 009 — a stale entry's last real duration is still a real
    data point about that task's typical cost, same reasoning `book_summary()`
    already applies to cost/duration totals). Slug-keyed, not a flat list, so
    each candidate's peer average can exclude its own value (see `_peer_average()`).
    """
    by_task = {}
    for s in summaries:
        for key, entry in s["tasks"].items():
            if entry.get("status") in ("done", "stale") and entry.get("duration_seconds") is not None:
                by_task.setdefault(key, {})[s["slug"]] = entry["duration_seconds"]
    return by_task


def deviation_flags(summaries: list, config: dict) -> dict:
    """Cyberstride-style outlier flags (ADR 010) — peer-comparison against the
    rest of the current portfolio, not a budget (this project has no plan/
    budget figure anywhere). A human sees these only when they run
    `s3 dashboard`, never pushed or alerted (this project's standing "no
    background process" rule, ADR 003). Returns
    {"cost": {slug: (value, peer_average, multiplier)},
     "duration": {(slug, task_key): (value, peer_average, multiplier)}} —
    empty dicts if `s3.deviation` isn't configured, or a sample is too small.
    """
    settings = config.get("s3", {}).get("deviation")
    if not settings:
        return {"cost": {}, "duration": {}}

    cost_flags = {}
    known_costs = {s["slug"]: s["cost_usd"] for s in summaries if s["cost_usd"] is not None}
    threshold = settings.get("cost_multiplier")
    if threshold and len(known_costs) >= _MIN_SAMPLE:
        for slug, value in known_costs.items():
            peer_avg = _peer_average(known_costs, exclude=slug)
            if peer_avg > 0 and value > threshold * peer_avg:
                cost_flags[slug] = (value, peer_avg, value / peer_avg)

    duration_flags = {}
    threshold = settings.get("duration_multiplier")
    if threshold:
        for task_key, durations in _task_duration_baseline(summaries).items():
            if len(durations) < _MIN_SAMPLE:
                continue
            for slug, value in durations.items():
                peer_avg = _peer_average(durations, exclude=slug)
                if peer_avg > 0 and value > threshold * peer_avg:
                    duration_flags[(slug, task_key)] = (value, peer_avg, value / peer_avg)

    return {"cost": cost_flags, "duration": duration_flags}
