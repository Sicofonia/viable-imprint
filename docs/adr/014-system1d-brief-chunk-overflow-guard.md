# ADR 014 — Brief/Digest Chunk-Overflow Guard (and a Specified, Deferred Map-Reduce Follow-Up)

**Status:** Decision 1 (the overflow guard) is Implemented — built and tested end-to-end against real invocations, see "Implementation notes." Decision 2 (map-reduce synthesis) remains fully specified here but deliberately deferred — see Consequences.

---

## Context

Investigating a suspected content-loss bug in a real book's `press-dossier` output surfaced a real, silent data-corruption bug affecting most of System 1D's "expansion" tasks (ADR 002), plus analogous tasks in Systems 4 and 5, for sufficiently long input.

Twelve tasks across three systems set `max_chars: 100000` specifically to avoid `llm_text`'s paragraph-boundary chunker (`lib/chunker.py`) splitting their input — each is documented, in its own `tasks.yaml` comment, as needing its input read **in one call**, because the whole point of the task is synthesizing one coherent document from the whole thing at once:

- **System 1D** (ADR 002, ADR 008, ADR 012): `synopsis`, `story-map`, `one-pager`, `press-dossier`, `trailer-storyboard`, `goodreads-profile`, `newsletter`, `article-draft` — all read `brief` (or, for `newsletter`, a scan digest) whole.
- **System 4** (ADR 003, ADR 012): `briefing`, `content-strategy` — read a combined scan / a briefing whole.
- **System 5** (ADR 006, ADR 007): `evaluate`, `homeostat` — read a candidate description / a combined S3+S4 confrontation whole.

ADR 002 named this exact risk explicitly when it introduced `max_chars`:

> "...they read the (much smaller) brief and must synthesize it in one call, not have it silently re-chunked into several unrelated partial documents if a very long book's brief happens to exceed 8,000 characters... each expansion task sets `max_chars: 100000`... comfortably above any realistic brief size."

That assumption held until now. A real book's `s1d/brief/es/source.txt` reached **460,733 characters** — `chunk_by_paragraphs()` splits it into `ceil(460733/100000) = 5` chunks with no error, no warning, and no code anywhere aware that this task was never supposed to receive more than one. `engines/llm_text.py`'s `run()` (lines 71-75) then does exactly what it always does: runs the task's prompt independently against each of the 5 chunks and joins the 5 results with `"\n\n"`.

The result, confirmed by direct inspection, is not one press dossier — it's five complete, self-contained, mutually inconsistent press dossiers concatenated into one file, each written by a model that only saw a fifth of the brief and had no way to know it. The same 5x duplication was independently confirmed in `one-pager` (5 copies of its title heading), `story-map` (5 copies of its `## 1. Introducción` heading), `trailer-storyboard` (8 scene tables instead of 2), and `synopsis` (output length ~4-5x a single synopsis). One chunk's `press-dossier` even names the wrong author (Folke Bergman instead of Sven Hedin) in its biography section — a direct consequence of that chunk's slice of the brief happening to be weighted toward a passage discussing Bergman, with no visibility into the rest of the book available to catch the error.

This is not an LLM defect: each per-chunk completion did exactly what its prompt asked, correctly, given what it could see. The defect is that a documented, load-bearing assumption ("this input always fits in one call") has no enforcement anywhere in the code that depends on it — so it fails silently, producing well-formed, plausible-looking, and wrong output instead of an error.

---

## Decision

### 1. A guard that fails loud instead of silently multiplying output — build now

Add an explicit boolean field, `single_chunk: true`, to any `tasks.yaml` entry whose task requires its entire input to reach the LLM in one call. This turns the assumption ADR 002/003/006/007/008/012 each already relied on in prose into data the engine can check, instead of a comment a human has to remember. Every task listed in Context gets the new flag:

```yaml
- name: press-dossier
  engine: llm_text
  prompt: prompts/s1d/press_dossier_task.txt
  max_chars: 100000
  single_chunk: true
  ...
```

`engines/llm_text.py`'s `run()` gains a matching `single_chunk: bool = False` parameter — no change needed in `lib/task_loader.py`, since manifest fields beyond the reserved set already flow generically into `engine.run(**extra_params)` (ADR 001's engine interface; see `_RESERVED_KEYS` in `lib/task_loader.py`). After chunking, before any LLM call is made:

```python
if single_chunk and len(chunks) > 1:
    raise click.ClickException(
        f"{prompt} requires a single call ({len(raw_text)} chars > "
        f"max_chars={max_chars}), but chunking produced {len(chunks)} chunks. "
        f"This task cannot synthesize a coherent document from partial input — "
        f"see docs/adr/014-system1d-brief-chunk-overflow-guard.md."
    )
```

Checked before the loop that calls the LLM, not after — a violation costs nothing (no tokens spent, no partial file written) and is caught at the first sign of trouble, not after burning several API calls to produce something that looks fine until read closely.

`cleanup`/`ortho`/`copyedit`/`brief` (and any future task correctly designed to process a long document chunk-by-chunk) never set `single_chunk`, so this changes nothing about their behavior — chunking is the intended, correct outcome there.

### 2. Map-reduce synthesis for long inputs — specified, deliberately deferred

The guard in Decision 1 converts silent corruption into a loud failure; it does not let a long book actually get a press dossier. The book that surfaced this bug, and any future book whose brief exceeds 100,000 characters, will hit the guard and stop, every time, until this is built.

**Design, for when this is picked up:** `llm_text.run()` gains an optional `reduce_prompt: str` parameter, usable only alongside `single_chunk: true`. When chunking produces more than one chunk:

1. Run the existing per-chunk pass exactly as today — one completion per chunk, using the task's normal prompt. This already happens; nothing changes here.
2. If `len(chunks) > 1` and `reduce_prompt` is set, make one additional LLM call: `reduce_prompt` as the system prompt, the N partial drafts (each labeled, e.g. `--- Borrador 1 de 5 ---`) as the user content. This reduce prompt's job is reconciliation, not fresh synthesis — pick one consistent title/tagline/author framing, merge overlapping sections, resolve contradictions (like the Bergman/Hedin mismatch) using the fact that, unlike any single map-step call, it can see everything the map step collectively produced.
3. If `reduce_prompt` is unset, fall back to Decision 1's hard failure — a task doesn't get silently-degraded map-reduce behavior just because the flag exists elsewhere; reduction must be explicitly authored and tested per task.
4. If chunking produces exactly one chunk (the common case for most books), skip the reduce call entirely — no added cost when it isn't needed.

Each task that wants this gets its own `reduce_prompt` file, following this project's standing one-prompt-per-task convention (ADR 001) rather than a shared generic reducer — `press-dossier`'s reconciliation needs (heading fidelity against a fixed template) are not `story-map`'s (waypoint-list merging and de-duplication across chunks) or `synopsis`'s (picking one 150-250 word synthesis out of several drafts).

**Why deferred rather than built alongside Decision 1:** it doubles the LLM cost of any task that trips it (map pass + reduce pass), needs its own prompt-reliability testing per task (this project's established pattern — every new multi-section prompt shape so far has needed at least one real-call iteration, per ADR 002 points 6-7 and ADR 006 points 9-10), and only twelve tasks are even candidates, most of which will never trip the guard in practice. Building it speculatively for all twelve before any of them has actually hit the guard would be exactly the kind of premature abstraction this project avoids elsewhere. The guard makes the failure visible and safe; map-reduce gets built task-by-task, starting with whichever task actually needs it first — almost certainly `press-dossier`, `one-pager`, `story-map`, or `synopsis`, since the book that surfaced this bug already needs all four.

---

## Alternatives Considered

- **Just raise `max_chars` further** (e.g. to 1,000,000) — rejected. Doesn't fix anything, only moves the threshold; brief size scales with manuscript length with no ceiling, so a long-enough book eventually hits it again, silently, exactly as today.
- **Compress/summarize the brief before it reaches expansion tasks** — considered: a "digest" pass between `brief` and the expansion tasks, shrinking 460K characters down under 100K before any expansion task runs. Rejected for now: this is a lossy pre-reduction performed *before* any task-specific framing is known, so it risks discarding material one task needs and another doesn't (e.g. `story-map`'s waypoint-level detail vs. `synopsis`'s high-level arc) — a single shared digest can't serve both well. Map-reduce (Decision 2) keeps each task's own reduce pass working from the full per-chunk detail specific to that task's own map-step output, not a lossy shared intermediate.
- **Infer `single_chunk` from `max_chars` being "large"** (e.g. `>= 100000`) instead of an explicit flag — rejected: implicit, guesses intent from a number rather than stating it, and this project consistently prefers explicit manifest flags for task semantics (`book_scoped`, `metadata_footer`) over inferring behavior from unrelated config values.
- **"Fix" this by splitting the offending book into more, shorter volumes** — not a general fix: in the case that surfaced this bug, the work was already published as multiple volumes, and a single volume still exceeded the budget. Worth doing as an editorial matter on its own merits for a particular book, but doesn't address the underlying missing guardrail for the next long book, in this or any other imprint using this project.

---

## Consequences

**Easier:**
- A future long book hits a clear, immediate error naming the exact task, the exact character counts, and this ADR — instead of a silently corrupted deliverable discovered by chance during manual review, as happened here.
- The assumption ADR 002/003/006/007/008/012 already documented in prose becomes something the code actually checks, across every system that made it, not just System 1D.

**Harder / needs care:**
- Until Decision 2 is built (task by task), any book whose brief exceeds 100,000 characters cannot get `synopsis`, `story-map`, `one-pager`, `press-dossier`, `trailer-storyboard`, or `goodreads-profile` at all — this is a regression from today's (broken but silently "working") behavior into a hard block, for exactly the books where getting it right matters most. The book that surfaced this bug is already in this state once Decision 1 ships. The same applies to System 4/5 tasks if a future scan/briefing/homeostat confrontation ever exceeds 100,000 characters, though nothing observed so far suggests that's imminent for those.
- The affected book's existing `press-dossier`, `one-pager`, `story-map`, and `synopsis` outputs are already corrupted and must be discarded and regenerated once Decision 2 (or a manual workaround) exists for each — they are not automatically fixed by shipping Decision 1 alone.
- Each `reduce_prompt` is new prompt surface needing its own real-call reliability testing (heading fidelity, no fabricated reconciliation) before it can be trusted — the same iteration cost every other multi-section prompt in this project has needed at least once.

---

## Implementation Checklist

- [x] Add `single_chunk: bool = False` parameter to `engines/llm_text.py`'s `run()`; raise `click.ClickException` before any LLM call when `single_chunk` and `len(chunks) > 1`
- [x] Add `single_chunk: true` to the twelve affected `tasks.yaml` entries: `s1d` (`synopsis`, `story-map`, `one-pager`, `press-dossier`, `trailer-storyboard`, `goodreads-profile`, `newsletter`, `article-draft`), `s4` (`briefing`, `content-strategy`), `s5` (`evaluate`, `homeostat`)
- [x] Confirm no change needed in `lib/task_loader.py` (manifest field flows through `extra_params` automatically) — verify with one real invocation
- [x] End-to-end test: re-run `s1d press-dossier` against a real (460K-char) brief that already exceeds `max_chars`, confirm the guard fires with a clear message instead of producing output; re-run against a book with a normal-length brief, confirm no behavior change
- [x] Update README's `llm_text` engine description and the `s1d`/`s4`/`s5` task reference tables with the new `single_chunk` field
- [ ] (Deferred to a follow-up pass, not this checklist) Decision 2: `reduce_prompt` support in `llm_text.run()`, starting with whichever of `press-dossier`/`one-pager`/`story-map`/`synopsis` is prioritized first for the affected book

---

## Implementation notes (2026-08-31)

Built and tested end-to-end against real invocations, not simulated:

- **Guard fires correctly, at zero cost.** `pipeline.py s1d press-dossier` against the real, oversized brief that surfaced this bug (444,681 characters as read by Python — `wc -c`'s byte count of 460,733 differs because several multi-byte UTF-8 characters, e.g. accented vowels and «»,  count as one character but more than one byte) failed immediately with: `prompts/s1d/press_dossier_task.txt requires a single call (444681 chars > max_chars=100000), but chunking produced 5 chunks.` — exit code 1, no LLM call made (the check runs before the completion loop), no output file written or overwritten.
- **No behavior change for normal-length input.** The same command run against `books/test/s1d/brief/es/zayagan-chp1.txt` (9,829 characters, one chunk) completed normally with a real Mistral call, exactly as before this change.
- **`lib/task_loader.py` needed no changes**, confirmed by the two invocations above both going through the real CLI path (`pipeline.py s1d press-dossier <file>`), not a direct call into the engine — the manifest's new `single_chunk: true` field flowed through `extra_params` into `engine.run()` exactly as ADR 001's engine interface predicted.
- No bugs found in this pass — the guard behaved exactly as specified on the first real test, likely because the change is small and additive (one early-exit check ahead of existing, unmodified logic) rather than a new code path with its own failure modes.
- The affected book's own `press-dossier`, `one-pager`, `story-map`, and `synopsis` outputs remain the pre-existing corrupted files on disk — Decision 1 prevents the failure from recurring on the next run, it does not retroactively repair what's already there. Regenerating them is blocked on Decision 2 (or a manual workaround) per Consequences, and out of scope for this pass.
