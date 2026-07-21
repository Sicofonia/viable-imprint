# ADR 010 — System 3: Deviation Flagging (Portfolio Outliers)

**Status:** Proposed. Not yet implemented — for review before work starts.

---

## Context

ADR 005 gave System 3 a read-only dashboard over real captured metrics (duration, tokens/characters, API cost) — but it only *shows numbers*. `docs/vsm.md`'s System 3 responsibility is narrower and more specific than that: *"review metrics from each unit and detect deviations from the plan."* Today, "detecting a deviation" is still entirely a human act of eyeballing a table and noticing that one row looks bigger than the others. A private follow-up review of the implemented project (`~/.claude/viable-imprint-next-steps.md`) named this as the first half of item 3, in Beer's own vocabulary:

> **Outlier flagging (Cyberstride-style):** the dashboard shows numbers; the human still detects deviations by eyeball. Add a flagging pass to `s3 dashboard` (e.g. title cost > N× portfolio average; task duration drifting run-over-run). Thresholds configurable in `config.yaml`, same optional pattern as `pricing:`.

**Two honest scoping notes, both extending ADR 005's own established discipline of labeling proxies as what they actually are, not what they resemble:**

1. `vsm.md` says "deviations from *the plan*" — this project has no plan or budget figure anywhere (ADR 005's Context explicitly named this: *"there's no budget figure anywhere in this codebase to track a deviation against"*). What this ADR can actually build is **peer-comparison**: is this book, or this task, an outlier relative to the rest of the portfolio? That's Cyberstride's actual mechanism too — an algedonic signal fires when a variable exceeds a *statistical* band, not necessarily a hand-set target — so the review's own naming ("N× portfolio average") already points at peer-comparison, not budget-tracking. Worth being explicit that this ADR is not inventing a budget system.
2. **"Task duration drifting run-over-run" cannot literally mean *this task's own history over time*** — the run-state ledger (ADR 004) keeps exactly one entry per `<system>.<task-name>` key, overwritten on every re-run (including after ADR 009's `stale` mechanism: once a stale task actually reruns, its old `duration_seconds` is gone, replaced by the fresh value). There is no append-only history to compute a trend from without a bigger, out-of-scope change (a time-series log). What *is* available today, for free, is the same peer-comparison idea as point 1 applied at task granularity: is this book's `s1b.cleanup` duration an outlier compared to `s1b.cleanup`'s duration across the rest of the portfolio? This ADR builds that, and names it accurately rather than implying a historical trend it can't actually compute.

---

## Decision

### 1. Two flag types, both peer-comparison against the current portfolio, both surfaced in `s3 dashboard`

- **Cost outlier (book-level):** a book whose total `cost_usd` exceeds `cost_multiplier` × the portfolio's average book cost.
- **Duration outlier (task-level):** a specific book's specific task (e.g. `s1b.ortho` on `zayagan-chp1`) whose `duration_seconds` exceeds `duration_multiplier` × that same task name's average duration across every book that has completed it.

Both reuse data `lib/dashboard.py` already computes (`portfolio_summary()`'s list of `book_summary()` dicts, each carrying the full per-task ledger under `"tasks"`) — no new capture, no new engine, no new perpetual root. This is a pure analysis pass over data ADR 005/009 already capture.

### 2. Where the code lives: `lib/dashboard.py`, not a new module

```python
# lib/dashboard.py
_MIN_SAMPLE = 3  # below this, an "average" is too noisy to flag against — see point 4


def _task_duration_baseline(summaries: list) -> dict:
    """task_key -> every completed duration for that task across the whole
    portfolio (done/stale, ADR 009 — a stale entry's last real duration is
    still a real data point about that task's typical cost, same reasoning
    dashboard.py already applies to cost/duration totals)."""
    by_task = {}
    for s in summaries:
        for key, entry in s["tasks"].items():
            if entry.get("status") in ("done", "stale") and entry.get("duration_seconds") is not None:
                by_task.setdefault(key, []).append(entry["duration_seconds"])
    return by_task


def deviation_flags(summaries: list, config: dict) -> dict:
    """Cyberstride-style outlier flags (ADR 010) — a human sees these only
    when they run `s3 dashboard`, never pushed or alerted (this project's
    standing "no background process" rule, ADR 003). Returns
    {"cost": {slug: (value, average, multiplier)},
     "duration": {(slug, task_key): (value, average, multiplier)}} —
    empty dicts if `s3.deviation` isn't configured, or a sample is too small.
    """
    settings = config.get("s3", {}).get("deviation")
    if not settings:
        return {"cost": {}, "duration": {}}

    cost_flags = {}
    known_costs = [s["cost_usd"] for s in summaries if s["cost_usd"] is not None]
    if len(known_costs) >= _MIN_SAMPLE:
        avg_cost = sum(known_costs) / len(known_costs)
        threshold = settings.get("cost_multiplier")
        if threshold and avg_cost > 0:
            for s in summaries:
                if s["cost_usd"] is not None and s["cost_usd"] > threshold * avg_cost:
                    cost_flags[s["slug"]] = (s["cost_usd"], avg_cost, s["cost_usd"] / avg_cost)

    duration_flags = {}
    threshold = settings.get("duration_multiplier")
    if threshold:
        for task_key, durations in _task_duration_baseline(summaries).items():
            if len(durations) < _MIN_SAMPLE:
                continue
            avg_duration = sum(durations) / len(durations)
            if avg_duration <= 0:
                continue
            for s in summaries:
                entry = s["tasks"].get(task_key)
                if entry and entry.get("duration_seconds", 0) > threshold * avg_duration:
                    duration_flags[(s["slug"], task_key)] = (
                        entry["duration_seconds"], avg_duration, entry["duration_seconds"] / avg_duration)

    return {"cost": cost_flags, "duration": duration_flags}
```

`book_summary()`/`portfolio_summary()` themselves are unchanged — flagging is a separate read of their output, not folded into the metrics functions, matching ADR 005's own layering (`metrics.enrich()` is a separate step from capture; this is a separate step from aggregation).

### 3. Config: `s3.deviation`, optional, same pattern as `pricing:`

```yaml
# config.example.yaml
s3:
  # Optional. Omit to skip deviation flagging entirely — s3 dashboard still
  # shows every number, just without a flag on any of them. Both are
  # peer-comparison multipliers, not a budget: "this book/task costs N times
  # more than the rest of the portfolio typically does," nothing more.
  deviation:
    cost_multiplier: 3.0
    duration_multiplier: 3.0
```

Either field can be set independently — omitting `cost_multiplier` skips only book-cost flagging, and likewise for `duration_multiplier`. No pricing dependency: duration flagging works even with no `llm.pricing`/`translation.pricing` configured at all, since it compares durations, not costs.

### 4. The minimum-sample guard is hardcoded, not configurable

With one or two books, "the portfolio average" is either undefined or trivially close to the one data point being compared against it — flagging anything in that state is noise, not signal. `_MIN_SAMPLE = 3` (a book-cost average needs at least 3 books with known cost; a task-duration average needs at least 3 books that have completed that specific task) is a statistical-noise guard, not a business decision like the multipliers are, so it isn't exposed in `config.yaml` — keeping the config surface to the two numbers a publisher would actually want to tune.

### 5. Display: an inline marker plus a trailing summary line, in both dashboard views

**Portfolio view** — a flagged book's cost gets a trailing `!`, plus a summary line naming exactly why:

```
Book              Tasks done   API cost   Compute time   In-pipeline span
life-as-explorer  13/13        $0.42      4m 12s         2026-07-02 -> 2026-07-05 (3 days)
zayagan-chp1      9/13         $1.31 !    3m 05s         2026-06-20 -> 2026-07-10 (20 days)
new-title         13/13        $0.38      2m 50s         2026-07-09 -> 2026-07-11 (2 days)

Totals: $2.11 API cost, 35 tasks completed, avg $0.70/title, avg compute time 3m 22s/title

Flagged: zayagan-chp1 — API cost $1.31 is 3.1x the portfolio average ($0.42)
```

**Per-book view** — same idea at task granularity:

```
Task                    Duration    Provider/Model            Usage                  Cost
s1b.cleanup              12.4s      mistral / mistral-medium   2,750 tokens           $0.003
s1b.ortho                48.2s !    mistral / mistral-medium   9,410 tokens           $0.011
...

Flagged: s1b.ortho — 48.2s is 3.9x the portfolio average for this task (12.3s)
```

No flag lines print at all when `s3.deviation` isn't configured, or nothing crosses the threshold — the existing dashboard output is completely unchanged in that case, matching ADR 005's "cost is opt-in, duration/usage are captured either way" precedent for optional-but-absent config.

---

## Alternatives Considered

- **True historical run-over-run trend (this task's own duration over time)** — rejected as infeasible without a bigger change: the ledger is overwrite-per-key, not append-only (ADR 004/009), so there's no history to trend against today. An append-only per-task metrics log is a real possible future ADR if this granularity turns out to matter, but it's a materially bigger change than "add a flagging pass," which is what was actually asked for.
- **A hardcoded, non-configurable threshold** — rejected: the review item explicitly named `config.yaml`, "same optional pattern as `pricing:`" — a publisher's sense of what counts as an outlier will differ by portfolio size and genre mix, same reasoning pricing itself is user-maintained rather than a constant.
- **An automatic notification when a flag fires (email, desktop alert, etc.)** — rejected: this project has a standing "no background process" rule (ADR 003) and every existing signal (task failures, `edited_since_run`, now this) is pull, not push — visible only when a human runs the relevant command. A flag firing between two `s3 dashboard` runs, unseen until the next one, is consistent with how the rest of the CLI already behaves, not a gap being left open.
- **A separate `s3 flags` command instead of folding into `s3 dashboard`** — rejected: the review explicitly asked for "a flagging pass to `s3 dashboard`," and splitting it would mean checking two commands to get the full picture instead of one; the numbers and the flags on those numbers belong next to each other.
- **Excluding the flagged item itself from the average it's compared against** — considered (self-inclusion mildly skews a small-portfolio average upward), not adopted for v1: the minimum-sample guard (point 4) plus a conservative default multiplier already keep that skew small in practice, and self-exclusion adds real complexity (a different average per candidate, computed once per row instead of once per portfolio) for a correction that only matters at exactly the portfolio sizes the sample guard is already screening out. Worth revisiting only if real use shows it producing visibly wrong flags.
- **Making the minimum-sample size configurable alongside the multipliers** — rejected: it's a noise guard, not a business threshold a publisher has any real reason to tune; keeping it a documented constant keeps the config surface to exactly the two numbers the review named.

---

## Consequences

**Easier:**
- The exact gap the review named — "the dashboard shows numbers; the human still detects deviations by eyeball" — is closed for the two cases that matter most at this project's scale: one abnormally expensive book, one abnormally slow task.
- Zero new capture, zero new files, zero schema change to the ledger — this is entirely a read-side addition over data ADR 005 and ADR 009 already produce, matching this project's "reuse existing mechanisms before writing new code" rule about as closely as an ADR can.
- A publisher who wants stricter or looser sensitivity changes two numbers in `config.yaml`, no code.

**Harder / needs care:**
- Flags are peer-comparison, not plan-comparison — a portfolio where *every* book is expensive for a legitimate reason (e.g. consistently long manuscripts) will never flag anything, since there's no external "the plan says X" figure to compare against. This is a real, accepted limit, not an oversight (point 1) — worth remembering when reading a quiet dashboard as "nothing costs too much" rather than "nothing costs more than *usual*."
- The minimum-sample guard means a young portfolio (fewer than 3 books, or fewer than 3 completions of a given task) sees no flags at all, by design — not a bug if a publisher notices flags "start working" only after their third or fourth book.
- Self-inclusion in the average (point 5 of Alternatives) means one book's own outlier cost mildly pulls up the average it's compared against — a real book already at 4x the *true* peer average might display as flagged at "3.7x," not 4.0x. Immaterial at the conservative default multiplier, worth knowing if a publisher tunes the multiplier very tight.

---

## Implementation Checklist

- [ ] Add `_task_duration_baseline()` and `deviation_flags()` to `lib/dashboard.py` (point 2)
- [ ] Add the optional `s3.deviation` block (`cost_multiplier`, `duration_multiplier`) to `config.example.yaml`, with a comment clarifying these are peer-comparison multipliers, not a budget (point 3)
- [ ] Wire `deviation_flags()` into `pipeline.py`'s `s3_dashboard` command: inline `!` marker plus a trailing "Flagged: ..." line per flag, in both the portfolio view and the per-book view (point 5)
- [ ] End-to-end test against real/disposable scratch data: at least 3 books with deliberately different costs, confirm the outlier book flags and the others don't; confirm a task deliberately made to run long (or a synthetic ledger edit, if a real slow call isn't practical) flags correctly at the task level; confirm no flag lines print when `s3.deviation` is absent from config, and confirm behavior with fewer than 3 books (no flags, not a crash)
- [ ] Update README (System 3 section, and a note in Setup alongside the existing `pricing:` optionality note)
