# ADR 024 — Assisted Translation: Chunk, Translate in Claude Code, Merge

**Status:** Implemented on the branch (2026-10-28), pilot not yet run. Branch-only: this work stays on `feature/adr-024-assisted-translation` and is not planned for `main` for now, because it puts a person back inside a task the pipeline otherwise runs unattended (see **Consequences**). T1–T5 are built and tested offline; **T0 (the pilot) and T6 (a first real book) are the Editorial Director's to run**, and the draft prompt `prompts/s1b/translate_task.txt` is waiting to be reviewed together first. See **Implementation Notes**.

---

## Context

`s1b translate` has had two providers so far, and neither is usable for production now:

- **DeepL** (ADR 001): its free 500,000-characters-a-month allowance is gone for this account (ADR 022, Context).
- **eTranslation** (ADR 022): free and technically working, but not good enough. After reviewing the first 30 pages of *the-gobi-desert*, the Editorial Director judged it mechanical: too many words translated literally, and some words that do not exist in Spanish. A sample from the same book, chapter 2: "por el vendaval aullante sopló arena y arena en mi cara" for "for the howling gale blew grit and sand into my face". Later stages cannot fix this: `ortho` and `copyedit` correct typography and abbreviations, not meaning.

The Editorial Director already works in VS Code with Claude Code and expects its translations of this kind of prose to be publishable after review; the pilot (T0) tests that before any code is written. Translating there costs nothing beyond the existing subscription. The idea was first discussed on 2026-10-23 and parked until eTranslation had been tried on a whole book. It has now been tried on enough of one.

### The constraint that shapes everything below

**The pipeline cannot drive the Claude Code chat in VS Code.** The extension is not callable from a Python process, so there is no `translate(chunk)` call to wrap. A person has to type the request in the chat, and the translation happens in that conversation, outside any `pipeline.py` process.

A script could call the Claude Code CLI headlessly (`claude -p`) instead. That is a different design (see **Alternatives**) and is not what this ADR builds: the Editorial Director starts the translation from the chat. Once started, it must work through every chunk on its own. Reviewing about 150 chunks one by one in a single sitting is not an option; review happens afterwards, on the merged text, as it does today for every other stage.

So translation splits into three parts: the pipeline **prepares** the work, the person **translates** in the chat, and the pipeline **merges and checks** the result. The pipeline must be able to stop between the first and the third part without reporting a failure.

### What the code does today (checked before writing this)

- `engines/translation.py` chunks the input with `lib/chunker.chunk_by_paragraphs()`, calls `provider.translate(chunk)` once per chunk, joins the results with `\n\n`, and writes `s1b/translated/<lang>/<input name>`. Providers are picked by `translation.provider` in `config.yaml`.
- The ledger (`manifest.yaml`, `tasks:`) knows `done`, `failed` and `stale` (ADR 004, ADR 009). `lib/task_loader._run_recorded()` records `failed` for any exception and `done` for a return value. There is no status for "started, waiting on someone".
- `lib/orchestrator.ready_tasks()` treats every task that is not `done` as eligible to run, once its input is `done`. `run_book()` runs each ready task once per call and lists exceptions as failures.
- `prompts/*/*_task.txt` is already gitignored (`.gitignore`, line 19), so `prompts/s1b/translate_task.txt` would not be committed. `books/` is gitignored too.
- The cleaned *the-gobi-desert* is 730,335 characters. Its prologue and first chapter ("First Impressions", lines 1–101 of `s1b/cleaned/source.txt`) are 14,595 characters.

---

## Decision

### 1. A new translation provider, `assisted`, selected in `config.yaml`

`translation.provider: assisted` switches `s1b translate` to the flow below. `systems/s1b/tasks.yaml` does not change, so DeepL and eTranslation stay selectable exactly as they are now.

`assisted` cannot work through the existing provider interface (`translate()` returns a translation immediately), so `engines/translation.py` hands the whole run to a new module, `lib/assisted_translation.py`, when this provider is selected. That module owns the three operations: prepare, status and merge. The engine decides which one to run (Decision 4).

```yaml
translation:
  provider: assisted
  source_lang: EN
  target_lang: ES
  assisted:
    max_chars: 7000        # chunk size; see Decision 2
    model_note: ""         # free text recorded in the ledger, e.g. "Sonnet 5.5"
```

### 2. Prepare: split the cleaned text into numbered chunk files

On the first run, the engine splits the input with the same `chunk_by_paragraphs()` every other task uses, so a chunk never cuts a paragraph. It writes one file per chunk to a work folder next to the final output:

```
books/<slug>/s1b/translated/es/assisted/
  chunks.yaml      # source path, source sha256, max_chars, chunk count, per-chunk paragraph count, created_at
  glossary.md      # per-book terms: names, places, titles, recurring phrases (Decision 5)
  en/001.txt       # source chunks, never edited
  en/002.txt
  ...
  es/001.txt       # written in the chat; absent until translated
  es/002.txt
  ...
```

Sources and translations are in separate folders so `es/` shows at a glance what is done (its file count is the progress), and the chat never writes next to a source file.

`max_chars` defaults to 7,000 characters, about three printed pages. That is small enough that one subagent translates a chunk comfortably in a single turn (and a chunk that fails validation is cheap to redo), and large enough that *the-gobi-desert* is about 105 chunks rather than several hundred. The pilot (T0) can change the default.

### 3. Translate: one request in the VS Code chat, then every chunk unattended

Nothing in the pipeline runs during this step. The person opens a Claude Code chat in VS Code, picks the model, and asks once for every pending chunk, for example:

> Translate every pending chunk of the-gobi-desert following prompts/s1b/translate_task.txt.

From then on the chat works through the chunks in order without stopping to ask anything. A book is about 105–150 chunks, more than one conversation can hold, so the main chat does not translate them itself. It hands **each chunk to its own subagent** (Claude Code's Agent tool), one after another. Each subagent starts with a fresh, small context, translates one chunk following the steps below, writes the file and returns a one-line result. The main chat keeps only those one-line results, so it can get through a whole book in one request. Chunks run one at a time, not in parallel, so that each chunk can read the previous chunk's translation and the glossary as it stands.

The prompt file tells the main chat how to dispatch the chunks (in order, one subagent each, skip any chunk that already has a file in `es/`, never stop to ask) and tells each subagent what to do with its chunk:

1. Read `glossary.md` and the last paragraph or two of the previous chunk's translation in `es/` (if any), for consistent names and a continuous voice across chunk boundaries.
2. Read `en/NNN.txt` and write `es/NNN.txt` with the translation only: no preamble, no notes, the same paragraphs in the same order, and markup (`[i]…[/i]`, `[sc]…[/sc]`, `[FN: … [/FN]]`, `---`) kept exactly where it is.
3. Add any new proper noun, title or recurring phrase to `glossary.md` with the Spanish form chosen.

**The person does not review chunks as they are written.** They start the request and come back when it has finished. Consistency comes from the glossary and the previous chunk's tail, not from chat memory, so the result does not depend on how the work was split between subagents or chats.

**Stopping and resuming is normal.** A run can stop part-way: the plan's usage limit is reached, VS Code is closed, the connection drops. Every finished chunk is already a file on disk, so the person asks again in a new chat ("continue translating the pending chunks of the-gobi-desert") and it picks up at the first chunk with no file in `es/`. A chunk interrupted half-written is caught by merge (Decision 6) and simply redone.

**Review happens after merge**, on `translated/es/source.txt` or later outputs, the same way the Director reviews every other stage today. When a passage needs fixing, the person either edits the merged text directly or asks for those chunks again and re-runs `s1b translate` (Decision 6, last paragraph).

**The prompt is co-written and never committed.** `prompts/s1b/translate_task.txt` is gitignored by the existing `prompts/*/*_task.txt` rule. It is written in Spanish, like `ortho` and `copyedit`, and drafted together during T0. No example copy goes into `prompts/examples/`.

The prompt does the translation only. Typography stays with the stages that already handle it: `ortho` (Latin quotes, Roman numerals, foreign terms, number formatting) and `copyedit` (abbreviations). The prompt must not tell the model to do their work, so that those stages and their length guard (`min_output_ratio`, PR #51) keep working unchanged.

### 4. One task, two phases, and a new `waiting` ledger status

`s1b.translate` stays a single task in the graph. Each time it runs (by hand or from `s2 run`), the engine looks at the work folder and does one of three things:

| Work folder state | What the engine does | Ledger status |
|---|---|---|
| No `assisted/` folder | Prepare (Decision 2), print what to do next | `waiting` |
| Some chunks have no file in `es/` | Print progress (`37/105 chunks translated`) and the next range to ask for | `waiting` |
| Every chunk has a file in `es/` | Validate (Decision 6), then merge into `translated/es/source.txt` | `done`, or `waiting` with the list of chunks that failed validation |

`waiting` needs three small changes, each next to the code that handles the existing statuses:

- **Engine → recorder:** the engine raises a new `lib.task_loader.TaskWaiting(message, progress)` exception. `_run_recorded()` catches it before its generic `failed` handler and records `status: waiting` with `progress` (e.g. `37/105`), `work_dir` and `attempted_at`. It then re-raises it so callers know the task did not finish.
- **Orchestrator:** `run_book()` catches `TaskWaiting` separately from other exceptions, prints `[s2] s1b.translate is waiting: 37/105 chunks translated (books/…/assisted/)`, and lists the task under a new `waiting` key in its summary, not under `failed`. `ready_tasks()` needs no change: `waiting` is not `done`, so the task is offered again on the next `s2 run` (which is what re-checks the folder), and nothing downstream runs.
- **Status views:** `s2 status`, `_summarize()` and the terminal UI (`tui/services/state.py`, the book screen) show `waiting` with its progress, in its own colour, not as a failure. The CLI exits with code 0 when the only unfinished task is waiting.

Running `s1b translate` directly behaves the same way: it prepares, reports progress or merges, and records the same ledger entries.

### 5. The glossary is per book and lives in the work folder

`glossary.md` holds one term per line (`Inn of Harmonious Brotherhood → posada de la Hermandad Armoniosa`). Prepare creates it empty. Claude Code adds to it while translating (Decision 3, step 3), and the person can correct it at any time. When a term is corrected after some chunks are done, the person asks for those chunks to be checked again; merge does not try to detect this.

The glossary is a book artefact, not imprint-wide configuration. An imprint-wide list (common Chinese place names, period spellings) can come later, once two books show which terms actually repeat.

### 6. Merge validates before it writes anything

Merge writes `translated/es/source.txt` only if every chunk passes these checks. Otherwise it records `waiting` with the failing chunk numbers and the reason for each, and moves each failing translation to `es/NNN.rejected.txt`. Those chunks then count as pending, so the same "translate every pending chunk" request redoes exactly them.

- **Source unchanged:** the sha256 of the current cleaned input matches `chunks.yaml`. If `cleanup` was re-run after prepare, merge refuses and tells the person to run `s1b translate --restart` (Decision 7). Translating against an old source would silently undo the new cleanup.
- **Every chunk present and not empty.**
- **Same paragraph count** as the matching file in `en/` (split on blank lines, as the chunker does). This is the check that would have caught the dropped chunk fixed in PR #51.
- **Length within bounds:** Spanish usually runs longer than English (eTranslation's Spanish for this book is 9% longer than the cleaned English: 797,052 against 730,335 characters), so a translation outside 0.9–1.6 times its source length is flagged. The pilot tunes the bounds.
- **Markup balanced:** the same number of `[i]`/`[/i]`, `[sc]`/`[/sc]` and `[FN:`/`[/FN]]` as the source, and `---` lines in the same places.
- **No chat leftovers:** no first line that looks like a preamble ("Aquí está la traducción", "Traducción:", a Markdown heading or code fence that is not in the source).

After a successful merge, the work folder stays in place. It is the record of how the book was translated, and re-running `s1b translate` re-merges from it (for example after a chunk is edited by hand).

### 7. Two options on `s1b translate`

Both are added through the engine's existing `CLI_OPTIONS`, so they also appear in the terminal UI's command explorer:

- `--status`: print progress and the next range to ask for, without changing the ledger. A read-only check.
- `--restart`: rename the work folder to `assisted.old-<timestamp>/` and prepare again. It never deletes translations.

### 8. Running a book with `s2 run`

The full flow for one book, with the manual step in the middle:

1. `pipeline.py s2 run <slug>` runs `cleanup`, then reaches `translate`, which prepares the chunks and stops calmly: `s1b.translate is waiting: 0/105 chunks translated`. Nothing downstream runs.
2. In VS Code, ask once for every pending chunk (Decision 3) and leave it running. If it stops part-way, ask again in a new chat. `pipeline.py s1b translate <slug's cleaned source> --status` shows how far along it is, at any time.
3. `pipeline.py s2 run <slug>` again. Translate validates and merges, the ledger records `done`, and the run carries on through `ortho`, `copyedit` and `format` (and System 1D, unless `--only s1b`).

If validation fails in step 3, the run stops at translate again with the failing chunks listed. Merge moves each failing translation aside (to `es/NNN.rejected.txt`), so repeating step 2 with the same request redoes exactly those chunks and nothing else.

The ledger records `provider: assisted`, `model` from `translation.assisted.model_note`, `duration_seconds` of the merge run only, and no `cost_usd`. The chat's usage is not visible to the pipeline. `s3 dashboard` shows the task with no cost, as it already does for tasks without usage.

---

## What does NOT ship in this ADR — explicit, not silent

- **No automation of the chat.** The pipeline never starts, drives or reads a Claude Code conversation.
- **No headless `claude -p` driver.** See **Alternatives**. The work folder layout does not prevent adding one later.
- **No committed prompt or prompt example.** `prompts/s1b/translate_task.txt` stays local.
- **No quality score.** Validation checks structure (Decision 6), not translation quality. Quality is the person's review after merge, as for every other stage.
- **No per-chunk review step.** The chat never pauses for approval between chunks.
- **No imprint-wide glossary** (Decision 5).
- **No change to `ortho`, `copyedit` or `format`.** They read `translated/es/source.txt` as before.
- **No merge to `main`** until the Editorial Director decides otherwise.

---

## Alternatives Considered

- **A headless driver: the script calls `claude -p` once per chunk.** It would be fully automatic and could be a normal provider with no `waiting` status. Rejected for now: the Editorial Director wants to start the translation from the VS Code chat, and scripted use of a subscription login needs checking against the plan's terms first. With Decision 3 already unattended once started, the two differ mainly in who presses start. It remains the natural next step if starting from the chat proves awkward.
- **Per-chunk review in the chat.** Rejected: about 150 chunks would mean hours of review in one sitting. Review moves to the merged text.
- **One chat translating every chunk itself, without subagents.** Rejected: a whole book does not fit in one conversation. The chat would compact or run out part-way, and later chunks would be translated with less of the instructions in view.
- **An Anthropic API provider through `llm_text`.** The cleanest fit for this codebase: a normal provider, unattended, with usage and cost in the ledger. Rejected for now because it is billed per token, separately from the subscription, and the point of this ADR is to translate without a new running cost. Not costed here.
- **Separate `s1b translate-split` and `s1b translate-merge` commands.** Simpler code (no new status), but the person has to remember which command comes next, and `s2 run` would need `--only` gymnastics around the gap. Rejected in favour of Decision 4.
- **eTranslation with its glossary or LLM enhancement.** ADR 022's implementation notes already tried LLM enhancement for real (`domain: SPD`): it fixed some literal readings but introduced worse mistakes, and is not recommended. A glossary fixes names and terms, but the problem the Director found is literal translation throughout, which a glossary does not fix.
- **The free Z.ai `llm_text` route (ADR 021).** Unattended and free, but its translation quality on period prose is untested. It could be added to the T0 comparison at little cost.

---

## Consequences

- **The pipeline is no longer unattended end to end** when `assisted` is selected. That is the departure from the project's direction that keeps this branch-only. `waiting` is designed so the rest of the pipeline treats the gap as normal rather than as an error.
- **Translation quality depends on the model and the prompt**, checked by the person's review of the merged text, not chunk by chunk. A mistake repeated across the book (a wrongly chosen name in the glossary, say) is found later than with per-chunk review, and fixing it means redoing the affected chunks. The pilot (T0) shows how often that happens.
- **The person's time per book is small:** one request, possibly a second after an interruption, then the merge run. Elapsed time is set by the model's speed and the plan's usage limits, which the pilot measures.
- **`waiting` is a new ledger status that other code must handle.** Anything that counts statuses (the orchestrator summary, `s2 status`, `s3 dashboard`, the terminal UI) has to show it, or a waiting book will look stuck or failed. T3 lists every place found.
- **The usage limits of the Claude Code plan become a production constraint.** A book is roughly 730,000 characters in and a little more out, spread over many chats. The pilot shows how much of the plan one chapter uses.
- **A translation can be re-checked chunk by chunk afterwards**, because the work folder keeps every source chunk next to its translation. eTranslation's whole-document output could not be checked this way.

---

## Implementation Checklist

Each task is done when its own check passes. Tests run offline (CONTRIBUTING.md), with a fake work folder, not a real chat.

- [ ] **T0 — Pilot.** (Not done: it needs the prompt reviewed together and a real chat session, which the Director runs. T1–T5 were built before it, at the Director's request; a no-go would mean shelving the branch.) Co-write `prompts/s1b/translate_task.txt` (Spanish, local only). Split the prologue and chapter 1 of *the-gobi-desert* (lines 1–101 of `s1b/cleaned/source.txt`, 14,595 characters, two or three chunks) with a throwaway script. Translate them in the VS Code chat with a single request, using one subagent per chunk as in Decision 3, and confirm the chat gets through every chunk without stopping to ask anything. Compare with the same passage in the existing eTranslation output (`s1b/translated/es/source.txt`, lines 1–101). Record in this ADR: model(s) used, time spent, how much of the plan's usage it took, the paragraph-count and length-ratio results, and the Director's verdict. **Go or no-go for T1 onwards.**
- [x] **T1 — `lib/assisted_translation.py`:** prepare, status, validate and merge, as plain functions on a work folder. Unit tests for each validation rule in Decision 6, including a dropped paragraph, unbalanced `[i]`, a preamble line and a changed source hash.
- [x] **T2 — `TaskWaiting` and the `waiting` status** in `lib/task_loader._run_recorded()` and `lib/orchestrator.run_book()` / `_summarize()`. Tests: a waiting task is not `failed`, blocks its dependents, and is offered again on the next run.
- [x] **T3 — Show `waiting` everywhere statuses are shown:** `s2 status`, `s3 dashboard`, `tui/services/state.py` and the book screen. Find every place with `grep` for `"stale"` and `"failed"` first, and list them here.
- [x] **T4 — Wire the provider:** `translation.provider: assisted` in `engines/translation.py`, the `assisted:` config block in `config.example.yaml`, and `--status` / `--restart` options. A test runs `s2 run` twice on a fake book: first run stops at waiting, chunk files are filled in, second run merges and reaches `ortho`.
- [x] **T5 — Documentation:** README (the three-step flow from Decision 8), `docs/cheat_sheet.txt` (`--status`, `--restart`), and this ADR's implementation notes.
- [ ] **T6 — First real book:** *the-gobi-desert* end to end with `assisted`, run by the Editorial Director (the commands are given, not run for them).

---

## Implementation Notes (2026-10-28)

What was built, and where it differs from the plan above.

**Code**
- `lib/assisted_translation.py`: prepare, progress, `validate_chunk` / `validate_all`, `reject`, merge, and `run()`, which the translation engine calls when `translation.provider` is `assisted`. `engines/translation.py` gained `--status` and `--restart`; `--status` creates nothing.
- `lib/task_loader.py`: `TaskWaiting`. `_run_recorded()` records `status: waiting` with `message` (first line only, so `s2 status` stays one line), `progress` and `work_dir`. Typed by hand, a waiting task prints its message and exits 0 (`_run_task_from_cli`).
- `lib/orchestrator.py`: `book_status()` has a `waiting` row (`ready_to_continue`), `run_book()` returns a `waiting` list, and `complete` stays false while anything waits.
- `pipeline.py`: `s2 status` shows `[w]`, `s2 run` prints `[w]` lines *after* the `Done/Failed` block (the terminal UI parses that block, so its format did not change), `book list` calls a waiting book "in progress".
- `lib/metrics.py`: the translation branch of `enrich()` records `model` when the engine reports one (the `model_note`).
- Terminal UI: `[w]` mark and "Continue" button on the book screen (`tui/services/state.py`, `tui/screens/book.py`); the cost estimate says assisted translation makes no provider call (`tui/services/estimate.py`); the health screen knows the provider (`tui/services/health.py`).
- `providers/__init__.py`: `get_translation_provider()` has an `assisted` branch that raises a clear error, because a test pins the factory's provider names to the health table, and anything but `s1b translate` asking for an assisted provider is a mistake.

**Differences from the plan**
- **Separate `en/` and `es/` folders (2026-10-28, after the first experiment).** The plan (and the first build) wrote `NNN.en.txt` and `NNN.es.txt` side by side, 116 files in one folder after the three-chunk experiment on *the-gobi-desert*. Sources now live in `en/NNN.txt` and translations in `es/NNN.txt` (rejected ones as `es/NNN.rejected.txt`): `es/` shows progress at a glance and the chat never writes next to a source. Only the path helpers in `lib/assisted_translation.py` changed, plus the prompt, README and these notes; the tests go through the helpers and needed no edit. The experiment's work folder was moved to the new layout by hand (file moves only, index untouched).
- **Descriptive names are translated (prompt, same day).** The experiment's glossary kept names such as "Horseshoe Temple" and "River of Sand" in English, because the first prompt said to leave place names as in the original. The prompt now separates descriptive names (translated: Templo de la Herradura) from transcriptions and people's names (kept: Kashgar, Chien Lung), and tells the chat to correct any glossary line that leaves a descriptive name in English.
- **`--status` and the ledger.** The plan did not say how a read-only call avoids `_run_recorded()`. `TaskWaiting` has a `record=False` form: same stop, ledger untouched.
- **Chunk count.** On the real cleaned text of *the-gobi-desert* (checked on a scratch copy, nothing under `books/` touched), 7,000 characters gives **111 chunks**, 3,813–6,996 characters each (average 6,510), none tiny; 14 contain a `---` paragraph. The ADR's "about 105" was an estimate.
- **`waiting` after a re-run of a finished task.** Recording `waiting` replaces the task's earlier `done` entry, and its dependents stay `done` until the merge succeeds, at which point the existing cascade (ADR 009) marks them `stale`. No new invalidation code.
- **Not built:** a health check that `prompts/s1b/translate_task.txt` exists, and a count of waiting tasks in the book header of the terminal UI.

**Tests** (`tests/test_assisted_translation.py`, 18 cases, offline, about 20 s): every validation rule; prepare / pending / reject / changed-source on a temporary folder; the full flow through the CLI (`s2 run` stops at translate, chunks are filled in, the next `s2 run` merges and reaches copyedit); a rejected chunk being redone; `--status` leaving the ledger untouched; `--restart` keeping the old folder; the orchestrator summary. The non-UI suite (223 tests) and the affected UI modules (book status, book pipeline, health, estimate, docs) pass; the full UI suite was not run.

**Not verified**
- Anything involving a real chat: whether one request really gets through all 111 chunks with a subagent each, how much of the plan's usage it takes, and what the translation is like. The draft prompt in `prompts/s1b/translate_task.txt` has never been used.
- Validation thresholds (0.9–1.6 length ratio) are guesses; the first real chunks will show whether they flag good translations or let bad ones through.
