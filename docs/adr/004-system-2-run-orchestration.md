# ADR 004 — System 2: Book Run-State and Task Orchestration

**Status:** Implemented. Built and tested end-to-end against a real (disposable) scratch book — see "Implementation notes" for one bug found and fixed, and one pre-existing, unrelated quirk found and deliberately left alone.

---

## Context

`docs/vsm.md` defines System 2 as the mechanism that prevents conflicts and synchronises operations across System 1 — in a micro-imprint, explicitly needed because "the same people participate in several units of System 1... without explicit coordination, bottlenecks are inevitable." Its listed tools are a shared calendar, a Kanban board, standardised templates, and a differentiated communication channel.

None of that exists in the code. What exists today, per ADRs 001–003, is:

- `systems/s1b/tasks.yaml` and `systems/s1d/tasks.yaml`, each an ordered list of tasks, run one Click command at a time: `pipeline.py s1b cleanup <file>`, then `pipeline.py s1b translate <file>`, and so on — the user retypes the previous task's output path as the next task's input argument every time.
- `manifest.yaml`, which records a flat `key: path` per completed task, but nothing about what's still pending, and nothing about how one system's tasks relate to another's.
- An explicit, repeated design decision to *not* build this: ADR 001 deferred "run-all" for System 1B; ADR 002 deferred it again for System 1D's eight commands ("each ... is still run by hand, in order, per book"); ADR 003 ruled out any in-pipeline scheduler ("dumb and sequential... the filesystem is the record").

That was the right call each time it was made — this project's stated philosophy is a CLI with clean per-task commands and filesystem state, driven by a human, one step at a time. But a private strategic assessment of this project (reviewed separately, not part of the committed docs) named this directly as the largest gap between `vsm.md` and the code: *"there is no coordination layer (the eight S1D commands are run by hand, in order, from memory), no resource allocation, and no metrics capture at all... Without a measurement channel, S3 cannot exist."* It also named the tension explicitly: *"the 'dumb and sequential' philosophy is in deliberate tension with the autonomy goal... the tension resolves if you treat the pipeline as the autonomous system's hands, and plan a separate orchestration layer as its S2/S3."*

The assessment's recommended first step, which this ADR proposes: **"Build S2 first — a run-state per book (extend `manifest.yaml`) and a single orchestrating command or agent that knows the task graph and what's next; this converts the toolkit into a pipeline."** Everything else the assessment recommends after this (a measurement channel, an S5 policy agent, System 1C) is explicitly out of scope here — this ADR is coordination only, matching the assessment's own sequencing.

---

## Decision

### 1. Scope: a run-state ledger plus a coordinating CLI layer, not a scheduler

This ADR adds:
- A **run-state ledger** — a new `tasks:` block inside each book's existing `manifest.yaml`, recording per-task status (`done`/`failed`), where its output landed, and when.
- An explicit, declarative **task graph** — each task in `tasks.yaml` optionally declares which other task's output it reads (`input:`), replacing the tribal knowledge that today lives only in the user's memory and in ADR 002's CLI usage examples.
- A new **`s2` command group** — `pipeline.py s2 status <book_slug>` (read-only: what's done, what's next, what's blocked) and `pipeline.py s2 run <book_slug>` (executes every currently-unblocked task, across systems, stopping only where the graph itself stops).

It does **not** add a scheduler, a daemon, or anything that runs without an explicit invocation — `s2 run` is a command you type, same as every other command in this CLI, consistent with ADR 003's "no background process" rule. The assessment's own resolution of the autonomy-vs-dumb-and-sequential tension is the frame here: this is still the human's hand on the CLI, just no longer required to also *be* the task graph's memory.

### 2. Why extend `manifest.yaml` rather than add a new state file

`manifest.yaml` is already, functionally, a partial run-state — every engine's `manifest.update()` call is really saying "this task happened, here's its output." System 4 already needed a separate `_state.yaml` (ADR 003) for its own purposes, but that file tracks *external* state (last-seen feed items), not *task* state — a different thing. Splitting book run-state into a second file risks the two drifting out of sync for no benefit; `vsm.md`'s System 2 is literally "the current status of every book," which is what `manifest.yaml` should become the single source of truth for.

**The existing flat keys are not reused as the ledger.** They were never designed as a machine-readable status record — they're organic, per-engine, and inconsistent by documented necessity: `translate` records `translated_es` (language-suffixed, computed at runtime from config), `format` records `formatted_formatted` (a preserved legacy quirk per ADR 001), `cleanup` records `cleaned` (explicit override). Reverse-engineering "is this task done" from that naming would mean either cleaning up three engines' historical output-key conventions first (out of scope, risks real books' existing manifests) or writing fragile per-engine special-casing into the orchestrator. Instead, this ADR adds one new, uniform nested block:

```yaml
# existing flat keys — unchanged, still written by every engine exactly as today
cleaned: s1b/cleaned/zayagan-chp1.txt
copyedit: s1b/copyedit/es/zayagan-chp1.txt
translated_es: s1b/translated/es/zayagan-chp1.txt
...

# new — the run-state ledger, keyed uniformly as "<system>.<task-name>"
tasks:
  s1b.cleanup:
    status: done
    output: s1b/cleaned/zayagan-chp1.txt
    completed_at: "2026-07-05T14:02:11"
  s1b.translate:
    status: done
    output: s1b/translated/es/zayagan-chp1.txt
    completed_at: "2026-07-05T14:05:47"
  s1d.synopsis:
    status: failed
    error: "MistralAPIError: rate limited"
    attempted_at: "2026-07-05T15:02:10"
```

Every entry is keyed by the task's declared `name` in its system's `tasks.yaml`, not by whatever ad hoc key that task's engine happens to write to the flat namespace. This decouples the orchestrator entirely from those legacy inconsistencies — it never has to know that `translate`'s real key is `translated_es`.

### 3. The task graph: an explicit `input:` field, resolved against the ledger

ADR 001 originally described "convention over configuration" input resolution — chain from the previous task's output by default, override with `input:` when a task reads from something else. It was never actually built; every task's CLI command still takes an explicit file path typed by the user. This ADR builds it, for real, scoped to what the orchestrator needs:

- **Default (no `input:` declared):** the task's input is the previous task's output *in the same system's `tasks.yaml` list*, looked up in the ledger as `<system>.<previous-task-name>`. This covers System 1B's pure chain (`cleanup` → `translate` → `ortho` → `copyedit` → `format`) with zero manifest changes.
- **Explicit override — `input: <system>.<task-name>`:** for anything that doesn't chain from its immediate predecessor. System 1D needs this for every one of its tasks: `brief` reads System 1B's output (a cross-system reference), and every other System 1D task (`synopsis`, `story-map`, `one-pager`, `press-dossier`, `trailer-storyboard`, `goodreads-profile`, `metadata`) reads `brief` — none of them chain to each other, they fan out from one shared extraction step (ADR 002). Positional default-chaining would silently wire `one-pager` to read `trailer-storyboard`'s output, which is wrong; this is exactly the case ADR 001 anticipated needing an override for.
- **The first task in a system with no predecessor at all** (only `s1b.cleanup` today): no `input:` to resolve from the ledger — the orchestrator looks for a single file in `<book_root>/s1b/source/`. Zero files or more than one is an error (`s2 status`/`s2 run` report it as blocked with a clear message), matching the existing single-manuscript-per-book assumption already implicit in `init`.

Concretely, `systems/s1b/tasks.yaml` needs no changes at all. `systems/s1d/tasks.yaml` gains one line per task:

```yaml
tasks:
  - name: brief
    engine: llm_text
    prompt: prompts/s1d/marketing_extract_task.txt
    manifest_key: brief
    input: s1b.copyedit

  - name: synopsis
    engine: llm_text
    prompt: prompts/s1d/synopsis_task.txt
    max_chars: 100000
    temperature: 0.7
    input: s1d.brief

  - name: story-map
    engine: llm_text
    prompt: prompts/s1d/story_map_task.txt
    max_chars: 100000
    input: s1d.brief

  # one-pager, press-dossier, trailer-storyboard, goodreads-profile: same pattern,
  # each gains `input: s1d.brief`

  - name: metadata
    engine: metadata_doc
    metadata_config: templates/marketing_metadata.yaml
    input: s1d.brief
```

This is the whole schema change to existing manifests — additive, no engine code needs to change to support it.

### 4. Recording ledger entries: one shared hook, not per-engine changes

The ledger must stay accurate whether a task was run manually (`pipeline.py s1b cleanup <file>`) or through the orchestrator (`pipeline.py s2 run <slug>`) — a publisher who still prefers running one command at a time should get exactly the same `s2 status` output as one who used `s2 run`. So the recording hook lives in one function both paths call: `lib/task_loader.run_task(root, config, system, task, input_file=None, **cli_kwargs)`, extracted out of the old `_build_command` callback. The Click CLI callback resolves `input_file` from what the user typed and calls `run_task()`; the orchestrator resolves `input_file` from the ledger (see point 6) and calls the exact same function — neither path duplicates the engine-invocation or ledger-recording logic.

```python
def run_task(root, config, system, task, input_file=None, **cli_kwargs):
    name = task["name"]
    engine = importlib.import_module(f"engines.{task['engine']}")
    extra_params = {k: v for k, v in task.items() if k not in _RESERVED_KEYS}
    run_fn = (lambda: engine.run(root, system, task.get("output", name), config, **extra_params, **cli_kwargs)) \
        if getattr(engine, "CLI_ARG", "file") == "none" else \
        (lambda: engine.run(input_file, root, system, task.get("output", name), config, **extra_params, **cli_kwargs))
    return _run_recorded(run_fn, root, system, name)  # wraps run_fn(), records done/failed either way
```

`lib/manifest.py` gains `record_task(book_dir, key, **fields)`: loads the manifest, updates only `data["tasks"][key]`, saves — a targeted merge, unlike the existing `update()`, which would silently clobber the whole `tasks` block if called naively with a full replacement dict.

`input` was also added to `task_loader.py`'s `_RESERVED_KEYS` (alongside `name`/`engine`/`output`) — without this, every `tasks.yaml` entry's new `input:` field would be passed straight through to its engine's `run()` as an unexpected keyword argument.

### 5. `pipeline.py s2 status <book_slug>`

Read-only. Loads every registered book-scoped system's `tasks.yaml` (System 1B, System 1D — see point 7 on System 4's exclusion), the book's ledger, and prints one line per task: done (with output path), ready (dependency satisfied, not yet run), or blocked (dependency not yet satisfied, naming which one).

```
Book: zayagan-chp1 (books/zayagan-chp1)

System 1B — Editorial Production
  [x] cleanup    s1b/cleaned/zayagan-chp1.txt
  [x] translate  s1b/translated/es/zayagan-chp1.txt
  [x] ortho      s1b/ortho/es/zayagan-chp1.txt
  [ ] copyedit   ready
  [ ] format     blocked — waiting on s1b.copyedit

System 1D — Publication and Marketing
  [ ] brief              blocked — waiting on s1b.copyedit
  [ ] synopsis            blocked — waiting on s1d.brief
  [ ] story-map            blocked — waiting on s1d.brief
  ...
```

### 6. `pipeline.py s2 run <book_slug> [--only s1b|s1d] [--step]`

Executes every currently-ready task, then recomputes readiness and repeats, until a full pass makes no further progress — this is what "converts the toolkit into a pipeline": running it once against a freshly-`init`ed book with a source file dropped in walks the whole graph, both systems, without the user retyping a single path. `--only` restricts to one system (e.g. finish System 1B, review the copyedit by hand, then run System 1D separately). `--step` runs exactly one ready task and stops, for a publisher who wants to pace it task-by-task but without hand-typing paths.

Independent siblings don't block each other on failure: if `synopsis` fails, `press-dossier` and the other System 1D fan-out tasks (all depending only on `s1d.brief`, not on `synopsis`) still run. A chained failure (e.g. `copyedit` fails) naturally blocks everything downstream of it, since nothing declares it ready without that dependency's ledger entry. `s2 run` prints a summary at the end (N done, N failed, N still blocked) and exits non-zero if anything failed. A `failed` task is retried on the next `s2 run` — no separate retry command; fixing whatever caused the failure (an API key, a rate limit passing) and re-running is enough, since "not done" is the only thing readiness checks for.

### 7. System 4 is out of scope for this orchestrator

System 4 isn't book-scoped (ADR 003) and is only two tasks (`scan` → `briefing`), already a straight chain a publisher runs by hand on a weekly cadence — the coordination pain point named in the assessment ("eight S1D commands run by hand") doesn't really exist there. Extending `s2` to cover it later is possible (same ledger mechanism would work against `intelligence/manifest.yaml` unchanged) but isn't needed now; scoping it in speculatively would be exactly the kind of premature generalization this project has otherwise avoided.

### 8. No backfill for pre-existing books

`books/test/` predates this ADR and has a fully-populated flat-key manifest with no `tasks:` block — but it's a test fixture, not a book in active production, so there's no real in-progress work whose ledger needs to be reconstructed. `s2 status`/`s2 run` therefore treat any book with no `tasks:` block (or a partial one) exactly as they would a book that simply hasn't run those tasks yet through the ledger-aware code path — no task is assumed `done` until it's actually been run (or re-run) after this ADR lands. If `books/test/` is still wanted as a working fixture afterward, the simplest path is re-running its existing commands once through the new code so the ledger populates naturally, same as any other book.

---

## Alternatives Considered

- **Reuse the existing flat manifest keys directly as run-state, no new ledger** — rejected (point 2): those keys are organically named per engine and already documented as inconsistent (language suffixes, the `formatted_formatted` quirk); building the orchestrator against them would either require fixing that history first or hard-coding per-engine exceptions into the orchestrator.
- **A separate `state.yaml` per book, mirroring System 4's `_state.yaml`** — rejected: System 4's state file tracks external world state (last-seen feed items); a book's run-state is intrinsically about the book, which is what `manifest.yaml` already is. Two files recording overlapping facts about one book invites drift.
- **A centralized, repo-root `pipeline.yaml` declaring the full cross-system dependency graph** — rejected: would move dependency knowledge out of the system that owns it, breaking ADR 001's "each system owns its own `tasks.yaml`" rule (a publisher forking System 1D shouldn't have to also edit a separate root-level graph file).
- **Fully automatic, unattended run-all (a daemon/watcher that runs `s2 run` on file-drop)** — rejected, contradicts ADR 003's explicit "no background process" design; `s2 run` stays an explicit, on-demand command, matching this project's standing philosophy. (An external OS-level scheduler calling it, the same pattern already used for System 4, remains an option outside this codebase's scope.)
- **Folding cost/duration/token metrics into the same ledger entries now** — rejected for this ADR specifically, even though it's an obvious next addition: the assessment names this as the *next* step after S2 ("add the measurement channel"), and keeping it separate keeps this ADR's diff to coordination only. The ledger's per-task dict is intentionally left open to additive fields (`duration_seconds`, `tokens`, `cost`) so that future ADR doesn't need a schema change, just new keys.
- **Backfilling `books/test/`'s ledger from its existing flat keys** — considered, rejected: it's a test fixture, not a book with real in-progress work to preserve, so there's nothing that needs reconstructing. Any book (including `books/test/`, if still wanted afterward) simply builds up its ledger going forward by having its tasks (re-)run through the new code path.
- **One task at a time only, no `s2 run --only`/`--step` pacing controls** — rejected: System 1D's expansion tasks cost real API tokens per run; a publisher reviewing `brief` by hand before letting seven downstream calls fire needs a way to stop there deliberately, not just an all-or-nothing switch.

---

## Consequences

**Easier:**
- A fresh book goes from "thirteen commands, typed by hand, in the right order, from memory" to `pipeline.py s2 run <slug>` (or `--step` through it) — this is the literal conversion from toolkit to pipeline the assessment asked for.
- `pipeline.py s2 status <slug>` answers "what's left on this book" without opening any folders — the closest thing this project has to `vsm.md`'s System 2 Kanban board, at zero infrastructure cost.
- Adding a task to either system's `tasks.yaml` automatically participates in orchestration — declare `input:` (or rely on positional chaining) and it's picked up by `s2 status`/`s2 run` with no orchestrator code changes.
- Sets up the assessment's next recommended step cleanly: a metrics ADR can extend each ledger entry with cost/duration fields without restructuring anything here.

**Harder / needs care:**
- `input:` becomes load-bearing configuration, not just documentation — a typo'd reference (`s1d.breif`) must fail loud (task permanently "blocked," with the bad key named in `s2 status`) rather than silently resolving to nothing.
- Two records of roughly the same fact now exist per completed task (the legacy flat key, and the new ledger entry) — they must only ever be written through the one shared hook in `task_loader.py`'s callback; a future engine that calls `manifest.update()` directly without going through that callback would silently desync the ledger from reality.
- Any book whose tasks were run before this ADR lands (`books/test/` included) shows every task as not-yet-done in `s2 status` until it's (re-)run through the new code path — expected, per point 8, not a bug.
- `s2 run`'s "keep going on independent sibling failure" behaviour is a small, deliberate step toward autonomy (today, a failed `s1b copyedit` run just stops — you notice immediately, in your own hands). Worth the user explicitly agreeing this is desired before implementation, not just inferred from the assessment.

---

## Implementation notes (2026-07-05)

Built and tested end-to-end against a disposable scratch book (`books/s2-scratch-test/`, a two-sentence dummy manuscript, created and deleted within this same session — not part of the repo). Two findings:

### 9. `_summarize()`'s "complete" check initially ignored `failed` tasks — found by actually running it

The first real `s2 run` against the scratch book correctly ran ten tasks, hit one genuine failure (`s1b.format`, a pre-existing environment issue unrelated to this ADR — see point 10), and correctly *kept going* rather than stopping (`s1d.brief` and every fan-out task ran fine, since none of them depend on `s1b.format`). But the summary printed `Done: 11, Failed: 1` immediately followed by `Book complete — nothing left to run` — a contradiction, since a failed-but-retryable task is not actually done. The bug: `_summarize()`'s `remaining` check only looked for `status in ("ready", "blocked")`, omitting `"failed"`. Fixed by adding `"failed"` to that check. Re-running against the same book confirmed the fix: `format` was retried (not skipped), failed again with the same error, and this time no "Book complete" message appeared alongside `Failed: 1`. `s2 status`'s own per-task display was correct throughout (`[!] format failed (ready to retry)`) — only the end-of-run summary had the bug.

### 10. A pre-existing, unrelated quirk found and deliberately left alone

The scratch book's `s1b.format` failure was `Template not found: templates\book_template.odt` — this environment's `templates/format_styles.yaml` (a gitignored, personal config file, per ADR 001) points at a template filename that isn't present here (only `templates/6x9template.odt` is). This is a local environment/config mismatch that predates this ADR and has nothing to do with orchestration — `s2 run` surfaced it clearly and moved on, exactly as designed, rather than masking it. Not fixed here, out of scope.

Separately (not a failure, just an observation): the scratch book's `manifest.yaml` ended up with `source: null` rather than the actual source file's path, even after tasks ran successfully. This traces to `init` writing a literal `source: ~` placeholder and `engines/llm_text.py`'s existing `"source" in existing` check (added before this ADR, see `engines/llm_text.py`) treating that placeholder as "already recorded" and never overwriting it with the real path. This is a pre-existing latent quirk in the legacy flat-key bookkeeping, independent of the new ledger (which never reads or writes the `source` key, and worked correctly regardless) — left alone as out of scope for this ADR.

---

## Implementation Checklist

- [x] Add `record_task(book_dir, key, **fields)` to `lib/manifest.py` — targeted merge into `data["tasks"][key]`, distinct from the existing whole-key `update()`
- [x] Add `depends_on` resolution to `lib/orchestrator.py` — read a system's `tasks.yaml` in order, resolve each task's effective input key (explicit `input:`, else previous task in list, else `None` for a first task)
- [x] Extract `run_task()` out of `lib/task_loader.py`'s `_build_command` callback, wrapped by `_run_recorded()` to call `manifest.record_task()` on both success and failure (point 4) — shared by manual CLI invocation and the System 2 orchestrator
- [x] Add `input` to `task_loader.py`'s `_RESERVED_KEYS` so the new field isn't passed through to engines as an unexpected kwarg
- [x] Add `input: s1b.copyedit` to `brief` and `input: s1d.brief` to every other task in `systems/s1d/tasks.yaml` (point 3) — no changes needed to `systems/s1b/tasks.yaml`
- [x] Build `lib/orchestrator.py`: `book_status(root)` (done/ready/blocked/failed per task), `ready_tasks(root, only=None)`, `run_book(root, config, only=None, step=False)`
- [x] Add the `s2` command group to `pipeline.py` (hand-written, not manifest-driven like `s1b`/`s1d`/`s4`): `status <book_slug>`, `run <book_slug> [--only s1b|s1d] [--step]`
- [x] End-to-end test: `init` a scratch book, drop a source file, `s2 status` (confirmed `cleanup` ready, everything else blocked), `s2 run --step` (confirmed exactly one task ran and was ledgered), full `s2 run` (confirmed ten tasks succeeded, one failed without blocking unrelated siblings, correct non-zero exit); re-ran `s2 run` again and confirmed the failed task retried rather than being skipped — found and fixed the point-9 bug in this pass; scratch book deleted afterward, not committed
- [x] Update README with the System 2 section and command reference (Architecture, the book-folder `manifest.yaml` description, and a new "Let System 2 run the whole pipeline for you" subsection in Running the Pipeline)
