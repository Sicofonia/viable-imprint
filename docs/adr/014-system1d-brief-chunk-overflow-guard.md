# ADR 014 — Brief/Digest Chunk-Overflow Guard and Map-Reduce Synthesis

**Status:** Implemented. Decision 1 (the overflow guard) shipped first; Decision 2 (map-reduce synthesis) shipped as a follow-up, wired to the four tasks that actually needed it (`synopsis`, `story-map`, `one-pager`, `press-dossier`) and tested end-to-end against the real oversized brief that surfaced this whole issue — see "Implementation notes" for both passes, including a real reconciliation defect found and fixed in `story-map`'s reduce prompt. The other eight `single_chunk` tasks (`trailer-storyboard`, `goodreads-profile`, `newsletter`, `article-draft`, `s4 briefing`, `s4 content-strategy`, `s5 evaluate`, `s5 homeostat`) still fall back to Decision 1's hard failure if they ever exceed `max_chars` — no `reduce_prompt` has been authored for them yet, per Decision 2's task-by-task, build-when-needed approach.

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

### 2. Map-reduce synthesis for long inputs — built as a follow-up, wired to four tasks

The guard in Decision 1 converts silent corruption into a loud failure; it does not, by itself, let a long book actually get a press dossier. This decision closes that gap for the tasks that actually needed it.

**As implemented:** `llm_text.run()` gained an optional `reduce_prompt: str` parameter, meaningful only alongside `single_chunk: true`. When chunking produces more than one chunk:

1. The existing per-chunk pass runs exactly as before — one completion per chunk, using the task's normal prompt. Unchanged.
2. If `len(chunks) > 1` and `reduce_prompt` is set, one additional LLM call reconciles the N partial drafts into a final document: `reduce_prompt` as the system prompt, the N drafts as the user content, each wrapped as `<borrador numero="i" total="N">...</borrador>`. **Not** the dash-delimited `--- Borrador 1 de 5 ---` label originally sketched here — `engines/feed_scan.py`'s `_wrap_source()` already documents why a Markdown-heading-like delimiter is the wrong choice: it visually primes a model to paraphrase its own fixed output headings into the same descriptive style instead of reproducing them literally (ADR 003 point 10). Since three of the four reduce prompts (`press-dossier`, `one-pager`, `story-map`) must reproduce fixed headings, `_wrap_draft()` reuses `feed_scan`'s XML-tag fix instead, for the same reason.
3. If `reduce_prompt` is unset, the task still falls back to Decision 1's hard failure — a task doesn't get silently-degraded map-reduce behavior just because the mechanism exists elsewhere; reduction must be explicitly authored and tested per task.
4. If chunking produces exactly one chunk (the common case for most books), the reduce call is skipped entirely — no added cost when it isn't needed.

Each task that wants this gets its own `reduce_prompt` file, following this project's standing one-prompt-per-task convention (ADR 001) rather than a shared generic reducer — `press-dossier`'s reconciliation needs (heading fidelity against a fixed template) are not `story-map`'s (waypoint-list merging and de-duplication across chunks) or `synopsis`'s (picking one 150-250 word synthesis out of several drafts). Four reduce prompts were written and wired up — `prompts/s1d/{synopsis,story_map,one_pager,press_dossier}_reduce_task.txt`, each with a committed English reference under `prompts/examples/s1d/` — matching the four tasks the book that surfaced this bug actually needed. The other eight `single_chunk` tasks (`trailer-storyboard`, `goodreads-profile`, `newsletter`, `article-draft`, both System 4 tasks, both remaining System 5 tasks) were left without a `reduce_prompt`: nothing has ever actually hit the guard for them, and per the "why built task-by-task" reasoning below, writing and testing a reduce prompt against a hypothetical failure is exactly the premature work this ADR avoids elsewhere. Any of them can gain one the same way, the day a real run actually needs it.

**Why built task-by-task, not for all twelve at once:** it doubles the LLM cost of any task that trips it (map pass + reduce pass), needs its own prompt-reliability testing per task (this project's established pattern — every new multi-section prompt shape so far has needed at least one real-call iteration, per ADR 002 points 6-7 and ADR 006 points 9-10 — and this pass was no exception, see Implementation notes), and most of the twelve candidate tasks will likely never trip the guard in practice. Writing and testing eight more reduce prompts against a failure mode that has never actually occurred for those tasks would be exactly the kind of premature work this project avoids elsewhere.

---

## Alternatives Considered

- **Just raise `max_chars` further** (e.g. to 1,000,000) — rejected. Doesn't fix anything, only moves the threshold; brief size scales with manuscript length with no ceiling, so a long-enough book eventually hits it again, silently, exactly as today.
- **Compress/summarize the brief before it reaches expansion tasks** — considered: a "digest" pass between `brief` and the expansion tasks, shrinking 460K characters down under 100K before any expansion task runs. Rejected for now: this is a lossy pre-reduction performed *before* any task-specific framing is known, so it risks discarding material one task needs and another doesn't (e.g. `story-map`'s waypoint-level detail vs. `synopsis`'s high-level arc) — a single shared digest can't serve both well. Map-reduce (Decision 2) keeps each task's own reduce pass working from the full per-chunk detail specific to that task's own map-step output, not a lossy shared intermediate.
- **Infer `single_chunk` from `max_chars` being "large"** (e.g. `>= 100000`) instead of an explicit flag — rejected: implicit, guesses intent from a number rather than stating it, and this project consistently prefers explicit manifest flags for task semantics (`book_scoped`, `metadata_footer`) over inferring behavior from unrelated config values.
- **A dash-delimited `--- Borrador 1 de 5 ---` label for wrapping map-step drafts in the reduce call** — this ADR's own original sketch, rejected once actually built: `feed_scan._wrap_source()` already found that a Markdown-heading-like delimiter primes a model to paraphrase its own fixed headings (ADR 003 point 10), a real risk for three of the four reduce prompts. Replaced with the same XML-tag wrapping `feed_scan` already uses, before this was ever tested against a real reduce call — a known failure mode applied proactively, not found the hard way a second time.
- **A single shared/generic `reduce_prompt` for every task, or a reduce prompt that dynamically adapts to any template** — not seriously considered: this project's established convention (ADR 001) is one prompt per task, independently tunable; a generic reducer would need to know each task's specific template rules (which headings, how many subsections, what to preserve verbatim) to do its job at all, which just relocates the per-task authoring into a more complex single file instead of eliminating it.
- **"Fix" this by splitting the offending book into more, shorter volumes** — not a general fix: in the case that surfaced this bug, the work was already published as multiple volumes, and a single volume still exceeded the budget. Worth doing as an editorial matter on its own merits for a particular book, but doesn't address the underlying missing guardrail for the next long book, in this or any other imprint using this project.

---

## Consequences

**Easier:**
- A future long book hits a clear, immediate error naming the exact task, the exact character counts, and this ADR — instead of a silently corrupted deliverable discovered by chance during manual review, as happened here — for any of the eight tasks without a `reduce_prompt` yet.
- For the four tasks that do have one (`synopsis`, `story-map`, `one-pager`, `press-dossier`), a long book now gets a genuinely coherent, single document instead of either silent corruption or a hard stop — confirmed against the real book that surfaced this bug, whose four previously-corrupted outputs are now regenerated and correct as a direct result of this pass's real-call testing.
- The assumption ADR 002/003/006/007/008/012 already documented in prose becomes something the code actually checks, across every system that made it, not just System 1D.

**Harder / needs care:**
- The eight tasks without a `reduce_prompt` (`trailer-storyboard`, `goodreads-profile`, `newsletter`, `article-draft`, both System 4 tasks, both remaining System 5 tasks) still hard-fail on any input exceeding `max_chars` — a regression from today's (broken but silently "working") behavior into a hard block, for exactly the books where getting it right matters most, until each one gets its own reduce prompt built and tested the same way the first four were.
- Each `reduce_prompt` is new prompt surface needing its own real-call reliability testing (heading fidelity, no fabricated reconciliation) before it can be trusted — confirmed to be a real, not theoretical, cost: `story-map`'s first real test surfaced a genuine reconciliation defect (duplicate waypoint blocks at chunk-draft boundaries), fixed by one round of prompt hardening — see Implementation notes.
- Map-reduce roughly doubles a task's LLM cost whenever it actually fires (map pass, already paid today, plus one reduce call) — real but bounded, since it only fires for books long enough to exceed `max_chars` in the first place, not on every run.

---

## Implementation Checklist

- [x] Add `single_chunk: bool = False` parameter to `engines/llm_text.py`'s `run()`; raise `click.ClickException` before any LLM call when `single_chunk` and `len(chunks) > 1`
- [x] Add `single_chunk: true` to the twelve affected `tasks.yaml` entries: `s1d` (`synopsis`, `story-map`, `one-pager`, `press-dossier`, `trailer-storyboard`, `goodreads-profile`, `newsletter`, `article-draft`), `s4` (`briefing`, `content-strategy`), `s5` (`evaluate`, `homeostat`)
- [x] Confirm no change needed in `lib/task_loader.py` (manifest field flows through `extra_params` automatically) — verify with one real invocation
- [x] End-to-end test: re-run `s1d press-dossier` against a real (460K-char) brief that already exceeds `max_chars`, confirm the guard fires with a clear message instead of producing output; re-run against a book with a normal-length brief, confirm no behavior change
- [x] Update README's `llm_text` engine description and the `s1d`/`s4`/`s5` task reference tables with the new `single_chunk` field

Decision 2 (follow-up pass):

- [x] Add `reduce_prompt: str = None` parameter to `engines/llm_text.py`'s `run()`; when set and chunking produces more than one chunk, run one additional reconciliation call over the map-step drafts instead of raising; `_wrap_draft()` delimits each draft with an XML tag (`<borrador numero="i" total="N">`), not a Markdown-heading-like label, per `feed_scan._wrap_source()`'s already-documented reasoning (ADR 003 point 10)
- [x] Write `prompts/s1d/synopsis_reduce_task.txt`, `story_map_reduce_task.txt`, `one_pager_reduce_task.txt`, `press_dossier_reduce_task.txt` (real, Spanish), each with a committed English reference under `prompts/examples/s1d/`
- [x] Wire `reduce_prompt:` into each of the four tasks' entries in `systems/s1d/tasks.yaml`, alongside their existing `single_chunk: true`
- [x] End-to-end test, all four tasks, against the real oversized brief that surfaced this bug: `press-dossier` and `one-pager` clean on the first real test; `story-map` surfaced a real reconciliation defect (see Implementation notes), fixed, and re-verified; `synopsis` clean on the first real test
- [x] Regenerate the affected book's four previously-corrupted `s1d` outputs (`press-dossier`, `one-pager`, `story-map`, `synopsis`) now that they can be produced correctly — done as a direct byproduct of the end-to-end tests above, not a separate step
- [x] Update README's `llm_text` engine description with the `reduce_prompt` mechanism and the `single_chunk` note after the task reference table
- [ ] Not built in this pass, left for whenever a real run actually needs one (per Decision 2's task-by-task reasoning): `reduce_prompt` for `trailer-storyboard`, `goodreads-profile`, `newsletter`, `article-draft`, `s4 briefing`, `s4 content-strategy`, `s5 evaluate`, `s5 homeostat`

---

## Implementation notes (2026-08-31)

Built and tested end-to-end against real invocations, not simulated:

- **Guard fires correctly, at zero cost.** `pipeline.py s1d press-dossier` against the real, oversized brief that surfaced this bug (444,681 characters as read by Python — `wc -c`'s byte count of 460,733 differs because several multi-byte UTF-8 characters, e.g. accented vowels and «»,  count as one character but more than one byte) failed immediately with: `prompts/s1d/press_dossier_task.txt requires a single call (444681 chars > max_chars=100000), but chunking produced 5 chunks.` — exit code 1, no LLM call made (the check runs before the completion loop), no output file written or overwritten.
- **No behavior change for normal-length input.** The same command run against `books/test/s1d/brief/es/zayagan-chp1.txt` (9,829 characters, one chunk) completed normally with a real Mistral call, exactly as before this change.
- **`lib/task_loader.py` needed no changes**, confirmed by the two invocations above both going through the real CLI path (`pipeline.py s1d press-dossier <file>`), not a direct call into the engine — the manifest's new `single_chunk: true` field flowed through `extra_params` into `engine.run()` exactly as ADR 001's engine interface predicted.
- No bugs found in this pass — the guard behaved exactly as specified on the first real test, likely because the change is small and additive (one early-exit check ahead of existing, unmodified logic) rather than a new code path with its own failure modes.
- The affected book's own `press-dossier`, `one-pager`, `story-map`, and `synopsis` outputs remain the pre-existing corrupted files on disk — Decision 1 prevents the failure from recurring on the next run, it does not retroactively repair what's already there. Regenerating them is blocked on Decision 2 (or a manual workaround) per Consequences, and out of scope for this pass.

## Implementation notes — Decision 2 (2026-08-31)

Built and tested end-to-end against the same real, oversized brief (444,681 characters, 5 chunks) used for Decision 1, for all four tasks that got a `reduce_prompt`:

- **`press-dossier`: clean on the first real test.** The reduce call correctly produced exactly one instance of each of the four fixed headings, correctly substituted `{{book_facts}}` once, correctly appended the `contact_facts` footer once, and — notably — cleanly resolved the Bergman/Hedin author confusion that motivated this whole investigation: the merged biography correctly presents Sven Hedin as the author (matching `book_facts`) with Folke Bergman correctly demoted to a named secondary expedition member, not a hallucinated fix but an accurate reconciliation. 8 chronologically-ordered subsections in "El corazón de la historia" (within the prompt's 4-8 guidance), no leftover `<borrador>` tags, no code fences.
- **`one-pager`: clean on the first real test.** Exactly one title, 3 highlight bullets, a 4-paragraph summary correctly spanning the entire journey (not just one chunk's slice), single accurate author bio.
- **`story-map`: a real reconciliation defect found, fixed, and re-verified.** First real test: the merged "Itinerario del viaje" waypoint list (344 entries) contained at least two exact, immediately-consecutive duplicate blocks (e.g. a 4-line block — Ch'ien-fo-tung / Hsing-hsing-hsia / Lung-wang-miao / Río Su-to-ho — repeated back-to-back with nothing between the two copies), the classic chunk-boundary merge failure the reduce prompt's original wording addressed only partially. Investigated before concluding it was a defect at all: the same run's own "Cronología" section independently corroborated that the expedition genuinely revisited the Ta-ch'üan/Su-to-ho area twice (worded identically on two different dates, consistent with a search-and-repair detour), so a real revisit was plausible — but the specific pattern found (zero other waypoints between two identical copies of a multi-line block) was judged not to fit that explanation. Fixed by hardening `story_map_reduce_task.txt`'s (and its English reference's) REGLAS section with an explicit rule distinguishing the two cases: an *immediately consecutive* exact duplicate (no other waypoint between the two occurrences) is a merge artifact and must be collapsed to one; the same waypoint recurring *with other waypoints between the two occurrences* is a legitimate revisit and must be kept. Re-tested against the same real brief: zero immediately-consecutive duplicate lines remained (verified programmatically), all six headings still present exactly once, and the legitimate revisit (Hsing-hsing-hsia / Lung-wang-miao / Su-to-ho) was correctly preserved — now with other waypoints between its two occurrences rather than back-to-back. Waypoint count dropped from 344 to 316 between the two runs; two minor waypoints present in the first run's output (`Pozo de Bilcher`, `Deresun-khuduk`) are absent from the second, which could be the new rule working as intended or ordinary run-to-run variance in what the map step extracted (each run makes five fresh, independent API calls, not a deterministic replay) — not fully resolved either way, noted here rather than glossed over.
- **`synopsis`: clean on the first real test.** 210 words (within the required 150-250), single prose paragraph pair with no headings or bullets, correctly synthesizing the whole journey from the October 1933 departure through the 1935 conclusion in Kansu, not just one chunk's slice.
- This is this project's established pattern holding again (ADR 002 points 6-7, ADR 006 points 9-10): three of the four new reduce prompts were clean on their first real test, and the fourth needed exactly one round of hardening after a real defect surfaced — fixed by prompt wording alone, no code normalizer needed, consistent with every prior instance of this pattern in this project.
