# ADR 009 — System 2: Ledger Invalidation (Stale Tasks and Edited Output)

**Status:** Implemented. Built and tested end-to-end against a real (disposable) scratch book — see "Implementation notes" for one design point confirmed by a real run and one unrelated pre-existing bug found and deliberately left alone.

---

## Context

ADR 004 gave System 2 a run-state ledger (`manifest.yaml`'s `tasks:` block) that answers one question: *has this task completed?* It has never answered a second, equally important one: *is that completion still trustworthy?* A private follow-up review of the implemented project (`~/.claude/viable-imprint-next-steps.md`) named this directly, second in its priority list and flagged above every other open item as *"the likeliest source of a silently-wrong published book"*:

> The ledger tracks completion, not validity. Two problems: re-running an upstream task ... leaves all downstream tasks marked `done` against stale inputs ... [and] hand-edits to output files ... are invisible.

Concretely, two gaps exist in the code as it stands today:

**1. No invalidation cascade.** If a publisher fixes `prompts/s1b/cleanup_task.txt` and re-runs `pipeline.py s1b cleanup <file>` on a book already past `copyedit`, the ledger still shows `translate`, `ortho`, `copyedit`, and `format` as `done` — `s2 status` reports nothing wrong, `s2 run` has nothing left to do. The book gets published from the old `cleanup` output's descendants, silently. This isn't hypothetical or rare: System 1D's fan-out shape (ADR 002) makes it worse — re-running `s1d brief` after a `brief` prompt fix leaves all seven of `synopsis`, `story-map`, `one-pager`, `press-dossier`, `trailer-storyboard`, `goodreads-profile`, and `metadata` marked `done` against the old brief, and none of them chain from each other, so there's no single downstream task a publisher would even think to re-check.

**2. No hand-edit visibility.** This project's whole model is human-in-the-loop — a publisher opening `s1b/cleaned/<slug>.txt` and fixing an OCR artifact by hand, or touching up `s1d/brief/<slug>.txt`'s wording before letting the fan-out tasks read it, is expected, normal use, not a workaround. But the ledger has no idea it happened: `s2 status` shows `done` identically whether the file is exactly what the engine wrote or has been substantially rewritten since. There's no signal telling a publisher "you edited this — do the tasks that already read it need a second look?"

Both gaps share a root cause: `status: done` is currently a permanent, one-way flag. This ADR makes it a claim that can be revoked.

**Also named in the same review item, a smaller, separate guard:** ADR 004 documented a "dual-bookkeeping landmine" — the ledger must only ever be written through `task_loader`'s shared recording hook (`_run_recorded()`/`record_task()`), never by an engine calling `manifest.update()` directly, or the ledger silently desyncs from reality. Nothing currently enforces this; it has held so far by convention alone.

---

## Decision

### 1. A new ledger status: `stale`

`status: done` gains a sibling, `status: stale` — a task that *was* done, whose input has since been invalidated by an upstream re-run, and whose current output should not be trusted without re-running it. `stale` is distinct from `failed`: nothing went wrong with the stale task itself; its own last run succeeded against input that has since changed underneath it.

```yaml
tasks:
  s1b.cleanup:
    status: done
    output: s1b/cleaned/zayagan-chp1.txt
    completed_at: "2026-07-20T14:02:11"
    output_mtime: 1721484131.0
  s1b.translate:
    status: stale
    output: s1b/translated/es/zayagan-chp1.txt
    completed_at: "2026-07-20T14:05:47"   # preserved from its last real run
    output_mtime: 1721484347.0
    invalidated_by: s1b.cleanup            # new — which re-run caused this
```

A `stale` entry keeps every field from its last completion (`output`, `completed_at`, prior System 3 metrics) — it's downgraded, not erased — plus one new field, `invalidated_by`, naming the upstream task whose re-run triggered it.

### 2. The cascade: computed from the existing task graph, triggered by the existing shared hook

When a task completes successfully, every task that (transitively) depends on it — per the same `input:`/positional-chaining graph ADR 004 already builds — gets checked: if its ledger entry is currently `done`, flip it to `stale`. If it's already `stale`, `failed`, or was never run, leave it alone — cascading only ever *demotes* a `done` entry, never anything else.

This has to work identically whether the upstream task was re-run manually (`pipeline.py s1b cleanup <file>`) or via `s2 run` — exactly the same requirement ADR 004 point 4 already solved once for ledger *recording*, by putting it in the one shared hook both paths call. This ADR reuses that: `lib/task_loader.py`'s `_run_recorded()`, immediately after recording a task `done`, calls a new `orchestrator.cascade_invalidate(root, key)`.

```python
# lib/orchestrator.py
def _dependents_map() -> dict:
    """Reverse of `_load_graph()`'s `depends_on` — key -> the keys that
    directly declare it as their input. Used only for cascading staleness;
    `_load_graph()` itself stays forward-looking for readiness checks."""
    dm = {}
    for node in _load_graph():
        if node["depends_on"]:
            dm.setdefault(node["depends_on"], []).append(f"{node['system']}.{node['task']['name']}")
    return dm


def cascade_invalidate(root: Path, key: str) -> None:
    """After `key` completes, mark every task transitively downstream of it
    (per the book task graph) `stale` if it was `done`. A safe no-op for any
    key outside the book graph (System 4, System 5's candidate evaluation,
    the homeostat/newsletter periodic pipelines) — `_dependents_map()` simply
    has no entry for those keys, since `_load_graph()` never included them
    to begin with (ADR 004 point 7, ADR 008's `book_scoped` filter)."""
    dependents = _dependents_map()
    queue, seen = list(dependents.get(key, [])), set()
    while queue:
        dep_key = queue.pop(0)
        if dep_key in seen:
            continue
        seen.add(dep_key)
        manifest.mark_stale(root, dep_key, invalidated_by=key)
        queue.extend(dependents.get(dep_key, []))
```

`lib/task_loader.py` imports `orchestrator` for this one call — the reverse of today's direction (`orchestrator.py` imports `task_loader` for `run_task()`). A top-level `from lib import orchestrator` in `task_loader.py` would create a real circular import (both modules partially executed at load time); a **local import inside `_run_recorded()`** avoids it cleanly, since by the time any task actually runs, `pipeline.py` has already fully imported both modules. See Alternatives for why a bigger restructure (splitting the graph builder into its own module) was considered and rejected.

`lib/manifest.py` gains the matching write path:

```python
def mark_stale(book_dir: Path, key: str, invalidated_by: str) -> None:
    data = load(book_dir)
    tasks = data.setdefault("tasks", {})
    entry = tasks.get(key)
    if entry and entry.get("status") == "done":
        tasks[key] = {**entry, "status": "stale", "invalidated_by": invalidated_by}
        save(book_dir, data)
```

### 3. `stale` in readiness and `s2 status`/`s2 run`

`ready_tasks()` needs no change at all: its existing skip condition is `entry.get("status") == "done"` (not "anything other than absent") — a `stale` entry already falls through exactly like a never-run one, so a stale task with a satisfied dependency is picked up by `s2 run` automatically, same as before this ADR. In practice this means re-running `s1b cleanup` and then `s2 run <slug>` re-walks and re-runs the entire now-stale downstream chain in one call, the same way a fresh book does — no new "un-stale" command needed.

`book_status()` (and therefore `s2 status`) gains one new branch, parallel to its existing `failed` handling:

```
System 1B — Editorial Production
  [x] cleanup    s1b/cleaned/zayagan-chp1.txt
  [~] translate  stale (ready to rerun) — invalidated by s1b.cleanup
  [~] ortho      stale (blocked — waiting on s1b.translate)
  ...
```

`_summarize()` (used by `s2 run`'s "Book complete" message) adds `"stale"` alongside `"ready"`/`"blocked"`/`"failed"` to its remaining-work check — otherwise a book left with an un-rerun stale task after a `--step` or `--only`-restricted `s2 run` could wrongly print "complete."

### 4. Hand-edit visibility: `output_mtime`, compared at status time

At the same completion point that already writes `output`/`completed_at` (`_run_recorded()`'s success branch), also capture `output_mtime: output_file.stat().st_mtime`. No new file read — `stat()` is already effectively free next to the write that just happened.

`book_status()` computes one new boolean per `done`/`stale` row: `edited_since_run` — true if the output file's *current* mtime no longer matches what was recorded, meaning something touched it after the engine wrote it.

```
  [x] brief      s1d/brief/zayagan-chp1.txt (edited since run)
```

Purely informational, exactly as the review item specified — it does not change `status`, does not affect `ready_tasks()`, and does not trigger a cascade (an edit to `brief` doesn't retroactively know whether it invalidates the seven fan-out tasks that already read the old version; a human decides that, the same way they'd decide it today). A publisher sees the flag and judges whether the downstream tasks still hold up, same review discipline as always — this just makes the fact visible instead of silent.

### 5. The dual-bookkeeping guard: a runtime assertion in `manifest.update()`

`tasks:` must only ever be written through `record_task()`/`mark_stale()`. `lib/manifest.py`'s general-purpose `update()` (still used by six engines for their own flat keys — `cleaned`, `brief`, `s4_scan_latest`, etc.) now refuses to touch it:

```python
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
```

This project has no test suite (every ADR's "Implementation notes" section documents real, disposable-scratch-book testing instead, not an automated one) — a written test isn't a realistic option here without introducing a testing framework as an unrelated side effect of this ADR. A permanent runtime assertion, hit on every single `update()` call for the life of the project, is the cheaper and arguably stronger guard: it fails loud the instant any future engine (this project's own, or a fork's) tries to bypass the hook, rather than relying on a test someone has to remember to run.

---

## Alternatives Considered

- **Extract the graph builder into its own module (`lib/task_graph.py`) to avoid the local import** — considered, rejected as more invasive than the problem needs: it would also mean moving `BOOK_SYSTEMS` and updating `lib/dashboard.py`'s existing `from lib.orchestrator import BOOK_SYSTEMS` import. A single well-commented local import inside one function is a smaller diff for the same result; worth revisiting only if a third consumer of the graph shows up.
- **A dedicated `s2 invalidate <book_slug>` command instead of an automatic cascade** — rejected: it re-introduces exactly the "publisher has to remember to do something extra" failure mode this ADR exists to close. The cascade is cheap (a graph walk already computed elsewhere) and automatic is strictly safer than opt-in here.
- **Silently re-running downstream tasks automatically instead of marking them `stale`** — rejected: this project's standing rule is that `s2 run` is always an explicit, human-invoked command (ADR 003's "no background process," ADR 004's "not a scheduler"); auto-cascading the *execution*, not just the *invalidation flag*, would mean re-running paid LLM calls without the publisher choosing to. Marking `stale` and letting the next `s2 run` (still explicit) sweep it up preserves that line.
- **A content hash instead of mtime for edit detection** — considered: more precise (immune to a touch-without-edit false positive), but requires reading the full output file at both completion and every `s2 status` call. Given this is a single-operator local tool, not a multi-writer or version-controlled environment, mtime's cheap `stat()` was judged sufficient; a hash is a strict-superset upgrade available later if mtime false-positives turn out to matter in practice.
- **Letting a publisher mark a `stale` task "acknowledged, still fine" without re-running it** — rejected for this ADR: adds a second escape hatch (accept-as-is vs. rerun) where the review item only asked for visibility and correct blocking. If accepted-staleness turns out to be a real, recurring need, it's a small additive follow-up, not a reason to hold this one back.
- **Applying the cascade/mtime mechanism to the periodic pipelines (homeostat, newsletter) too** — rejected: both already unconditionally re-run their entire chain on every invocation (ADR 007/008), with no "already done" concept to invalidate in the first place. `cascade_invalidate()` is a safe no-op against their keys by construction (they're excluded from `_load_graph()`), not a gap left unaddressed.

---

## Consequences

**Easier:**
- The exact failure mode the review flagged as highest-risk — a book published from stale downstream artifacts after an upstream fix, with no indication anything was wrong — is closed. `s2 status` now tells the truth about whether `done` still means what it says.
- System 1D's fan-out shape, previously the *worst* case for this bug (seven siblings, no chain between them, easy to fix `brief` and forget all seven), becomes the *clearest* demonstration the mechanism works — one `brief` re-run visibly stales all seven at once.
- `s2 run` needs no new flags or modes to pick up stale work — it already re-runs anything not `done`, so the existing verb does the right thing once the ledger tells it the truth.
- The dual-bookkeeping guard converts an implicit convention (documented in prose since ADR 004, never enforced) into something that fails immediately and loudly if violated, by any engine, present or future.

**Harder / needs care:**
- Every ledger read that special-cased `status == "done"` needs to also consider `"stale"` where "has real output to show" matters (e.g. `s3 dashboard`, which currently only reads task entries generically — worth a quick audit during implementation that nothing there misreads `stale` as absent-or-broken rather than "done, but superseded").
- `output_mtime` comparisons are filesystem-timestamp-precision-dependent — fine for this project's single-operator, local-filesystem use, but would need revisiting (e.g. a hash) if `books/` were ever synced through something that doesn't preserve mtimes exactly (a naive cloud-sync tool, some archive formats).
- The local import in `task_loader.py` is a real, if narrow, layering inversion (task_loader → orchestrator, where today only orchestrator → task_loader exists) — worth a code comment strong enough that a future contributor doesn't "clean it up" into a top-level import and reintroduce the cycle.

---

## Implementation notes (2026-07-20)

Built and tested end-to-end against a disposable scratch book (`books/s2-invalidation-scratch/`, a three-sentence dummy manuscript, created and deleted within this same session — not part of the repo).

### 11. The cascade's `invalidated_by` traces to the root cause, not the immediate parent — confirmed as the more useful choice

`cascade_invalidate(root, key)` stamps every transitively-downstream entry with the *original* triggering `key`, not each hop's immediate predecessor. Concretely, after re-running `s1b.cleanup`, `s1d.metadata` (four hops downstream: `cleanup` → `copyedit` → `brief` → `metadata`) shows `invalidated by s1b.cleanup`, not `invalidated by s1d.brief`. Seeing this in a real `s2 status` run confirmed it's the right call: naming the immediate parent would just push the "why" question one hop down the chain (`brief` is itself stale for the same reason) rather than answering it. Root-cause attribution answers it directly.

### 12. `s3 dashboard` needed two small fixes beyond the ADR's own sketch, both about not silently dropping already-spent cost

Real-run testing surfaced that `stale` also had to be threaded through System 3's reporting, not just System 2's own status/readiness — the ADR's Consequences section flagged this as a risk to audit (point labeled "needs care") but hadn't named the exact fix. Two call sites in the existing code filtered strictly on `status == "done"`: `lib/dashboard.py`'s `book_summary()` (the aggregate cost/duration/task-count numbers) and `pipeline.py`'s per-task `s3 dashboard <book>` breakdown table. Left unfixed, the moment any task went `stale`, its already-incurred API cost and compute time would have silently vanished from both — a book's *reported* spend dropping the instant an upstream fix triggered a cascade, even though the money was actually spent. Both now count `stale` alongside `done` for cost/duration/task-count purposes (S3's job — money spent, not currency validity), while `s2`'s own readiness logic is unaffected (`stale` still isn't `done` there, correctly). The per-task table also labels a stale row `(stale)` inline so the two totals stay legible together.

### 13. A pre-existing, unrelated bug found and deliberately left alone

While reading `s3 dashboard` output during testing (`12/16 tasks done`), the total looked wrong — the scratch book only has 13 book-scoped tasks (5 in `s1b`, 8 in `s1d`). `lib/dashboard.py`'s `_total_task_count()` sums *every* task in each book-scoped system's `tasks.yaml`, without applying the `book_scoped` filter `orchestrator._load_graph()` already uses — so it's been counting System 1D's three newsletter tasks (`book_scoped: false`, ADR 008) as part of every book's total since ADR 008 merged, inflating the denominator from 13 to 16 for every book, in every `s3 dashboard` view. This predates ADR 009 and has nothing to do with invalidation — a one-line fix (`if t.get("book_scoped", True)`), but out of scope here per this project's own precedent (ADR 004 point 10 handled an unrelated quirk the same way: named clearly, not silently fixed inside an unrelated ADR's diff).

---

## Implementation Checklist

- [x] Add `mark_stale(book_dir, key, invalidated_by)` to `lib/manifest.py`; add the `"tasks" in kwargs` guard to `update()` (point 5)
- [x] Add `output_mtime=output_file.stat().st_mtime` to `_run_recorded()`'s success-path `record_task()` call in `lib/task_loader.py`
- [x] Add `_dependents_map()` and `cascade_invalidate(root, key)` to `lib/orchestrator.py` (point 2)
- [x] Call `cascade_invalidate()` from `_run_recorded()` via a local import, right after a successful `record_task(..., status="done", ...)` — comment explaining why the import is local, not top-level
- [x] Extend `book_status()`: generalize the existing `done` check to a `status` variable, add a `stale` branch (output, `invalidated_by`, `ready_to_rerun`) parallel to the existing `failed` branch, add `edited_since_run` to both `done` and `stale` rows via a new `_edited_since_run(root, entry)` helper
- [x] Add `"stale"` to `_summarize()`'s remaining-work status tuple
- [x] Update `pipeline.py`'s `s2 status` printer for the new `stale` row shape and the `edited_since_run` annotation
- [x] Audit `lib/dashboard.py` and `pipeline.py s3 dashboard` for any place that reads ledger `status` and would mishandle `"stale"` as something other than "done, but superseded" — found and fixed two (point 12); found one unrelated pre-existing bug, deliberately left alone (point 13)
- [x] End-to-end test against a disposable scratch book: ran the full S1B/S1D graph to completion (12/13 tasks — `s1b.format` hit the same pre-existing local template-path quirk ADR 004 documented, unrelated); re-ran `s1b cleanup` and confirmed `translate`/`ortho`/`copyedit`/`format` and, cross-system, `s1d.brief` and all seven of its fan-out dependents showed `stale` with the correct `invalidated_by` (point 11); confirmed `s2 run` re-walked and cleared the whole stale chain in one call, including after a `--only s1b`-restricted run left the S1D half deliberately stale in between; hand-edited a `done` output file and confirmed `s2 status` flagged `edited_since_run` without changing status or blocking anything, and that the flag survived the entry's transition to `stale`; confirmed a direct `manifest.update(root, tasks={...})` call raises `ValueError`; scratch book deleted afterward, not committed
- [x] Update README (the `manifest.yaml`/ledger description, the System 2 section, and the `s2 status` example output) to document `stale` and `edited_since_run`
