# ADR 002 — Two-Stage Marketing Pipeline (Extract-Then-Expand)

**Status:** Implemented.

---

## Context

System 1D's `marketing` task currently makes one LLM call per book: it sends a truncated manuscript (capped at 60,000 characters to bound cost) to a single prompt that returns three documents, split out of one response by matching heading text (`engines/llm_multi_doc.py`).

Two problems, both now confirmed:

1. **Coverage.** The imprint's manuscripts routinely run well beyond 60,000 characters once translated. A story map, a trailer beat-sheet, or a press dossier that only "sees" the first fraction of the book will silently misrepresent everything past the truncation point — this is an accuracy defect, not just a cost one.
2. **Scope.** The Editorial Director needs seven deliverables per book (synopsis, press dossier, one-pager, Goodreads profile, book metadata, an ArcGIS story-map data source, and a trailer storyboard), and different publishers running this project will want more, fewer, or different ones. Hardcoding seven headings into one prompt, the way three are hardcoded today, doesn't scale and isn't configurable — it repeats the exact problem ADR 001 already solved for System 1B: task identity baked into prose inside one file instead of expressed as data.

Sending the full manuscript once per downstream deliverable (7 calls, each re-reading the whole book) was considered and rejected on cost grounds: the manuscript is the expensive part of every one of these calls, and re-paying for it seven times buys no accuracy benefit five of the seven documents share the same marketing register and could work from the same distilled understanding of the book.

## Decision

Split `marketing` into two kinds of task, chained the same way System 1B's tasks already chain (explicit file-to-file, one task per CLI command, no automatic run-all):

1. **One extraction task (`brief`) reads the full manuscript, once.** It chunks the manuscript with the existing paragraph-boundary chunker and, per chunk, asks the LLM to pull out a small structured extract (events in order, place names with one-line context, notable quotes, tone, biographical facts) rather than to rewrite or transform the chunk. Because the extract per chunk is compact regardless of chunk size, concatenating all chunk extracts (no separate reduce call) still produces a document — the **brief** — that stays small however long the source book is. The brief is saved to disk like any other stage output, so it's inspectable and hand-editable before anything downstream runs.

2. **Every other marketing deliverable reads the brief, not the manuscript.** This is the whole cost win: the expensive input (the full book) is paid for exactly once, and adding an eighth or ninth deliverable later costs nothing on that axis. Five of these tasks (synopsis, press dossier, one-pager, Goodreads profile, trailer storyboard) are one LLM call each against the brief, using **`engines/llm_text.py` completely unchanged** — chunk (the brief is small enough it usually won't even split), call the LLM with a task-specific prompt, write the result. This gives the strict **1-to-1 task-to-prompt relationship** requested: each deliverable is its own `tasks.yaml` entry with its own prompt file, independently editable, addable, or removable, exactly like `cleanup`/`ortho`/`copyedit` are today. A publisher who only wants three of these seven simply doesn't list the other four.

3. **`story-map` turned out to need an LLM after all — revised after reviewing the user's actual story-map format.** The original plan (parse the brief's `<locations>` tag with plain code into a table) undersold what a story map actually is for this imprint: a six-section reference document — journey introduction, explorer biography, an ordered waypoint list, a dated chronology, notable temples/ruins, and notable ethnic groups. Critically, the waypoint list needs each historical toponym paired with its modern name where known (e.g. "Semipalatinsk (Semey)", "Kalgan (Zhangjiakou)") — that's gazetteer knowledge the brief doesn't contain and plain code can't supply, only an LLM's world knowledge can. So `story-map` is a sixth `llm_text` task like the others, with its own prompt (`prompts/s1d/story_map_task.txt`), reading the brief. The one hard rule carried over from the original design: the prompt explicitly forbids inventing geographic coordinates — a modern *name* in parentheses is verifiable/correctable by the editor before it reaches ArcGIS, a hallucinated lat/long is not, so the task only ever outputs place names, never coordinates.

4. **`metadata` is the only task left with no LLM call.** It reads a new per-book `marketing_metadata.yaml` (ISBN, price, publication date, page count, format — facts nobody should ask an LLM to guess) and merges it with a running description built from every `<overview>` tag in the brief, into `metadata.md`. This needs one small new engine (`engines/metadata_doc.py`) since its job is parsing/merging structured text, not calling a provider — but it costs nothing per run.

5. **Bibliographic/contact facts can now be injected into *any* `llm_text` task's output — discovered building `press-dossier`, generalized into `lib/metadata_blocks.py`.** The press dossier needs the same never-ask-an-LLM-to-guess facts as `metadata` (author, ISBN, price...) but interleaved into otherwise LLM-authored prose, at two different kinds of position:
   - **Mid-document, content-dependent position** (e.g. right after the introduction paragraph): the prompt leaves a literal `{{book_facts}}` token there; the engine substitutes it with a rendered bullet list after the LLM call. Tested reliable across repeated runs.
   - **Fixed position at the very end of the document** (a closing contact block): the same token approach was tried first (`{{contact_facts}}` as the prompt's last instructed line) and **failed in practice** — across identical runs the model consistently ended the document on the preceding blockquote and never emitted the trailing token, even after the prompt was strengthened to call it "MANDATORY" in capital letters. This is now handled as an unconditional code-side append (`metadata_footer: contact_facts` in the task's manifest entry) instead of an instruction the model has to remember to follow at the point it's naturally inclined to stop. Lesson: don't ask a model to place something it has no content-driven reason to place — if a block's position is fixed regardless of what the model writes, put it there in code.

6. **Prescribed Markdown headings need a "fill-in-the-template" framing, or the model quietly drops them — found while testing `one-pager`, `press-dossier`, and `goodreads-profile`.** Writing "`## Section Name` — a paragraph describing what goes here" as instructional prose is not reliably read as "reproduce this heading literally": across repeated runs the model would replace a prescribed heading with a `---` divider, or omit it entirely while still writing the section's content. Content the model chooses to title itself (e.g. `press-dossier`'s historical-context subsections) was never affected — only headings it was told to copy verbatim. Fixed by restructuring every multi-section prompt as an explicit template: literal headings on their own lines, section instructions moved *inside* `[bracketed placeholders]` immediately below each heading, plus a rule stating the headings are literal text to reproduce, not descriptions. This fixed `one-pager` and `goodreads-profile` outright. `press-dossier` needed one further iteration: it originally asked the model to *also* invent one heading of its own (a creative title for the historical-context section) positioned between two fixed ones — that mixed instruction reliably broke in three different ways (heading dropped, `### ##` doubled hashes from partially echoing the instruction's own `##` syntax, heading merged into the next fixed section). The fix was to stop asking for an invented heading at all: that section's `## El corazón de la historia` heading is now fixed like the rest, and creative variation per book is preserved as an *italic subtitle line* under it (not a heading level) — subtitles carry no structural risk the way heading levels do. General lesson: a model will reliably reproduce a heading it's shown literally, but mixing "reproduce this heading" with "invent your own heading" in the same template is a reliability trap — if any variation is wanted, put it in the content, not in the heading text itself.

7. **`marketing_extract_task.txt` needed XML tags instead of Markdown headings — the point 6 fix wasn't enough for this one prompt, found while building `metadata`.** Applying the fill-in-the-template fix from point 6 to the extraction prompt (which was written before that lesson existed) did not fix it: instead of dropping `## Overview`, the model paraphrased it into a different descriptive title synthesized from the section's own instructions, and did so *inconsistently between chunks of the same run* (e.g. "## Who is narrating, where, when, what is happening" in one chunk, a shorter variant in another). No single fixed alternate heading could have been hunted down and matched, since it wasn't stable. Switched all seven headings to XML-style tags (`<overview>`, `<events>`, `<locations>`, `<temples>`, `<ethnic_groups>`, `<notable_quotes>`, `<tone>`) instead, and every tag was reproduced exactly, in order, across all chunks, immediately — no further iteration needed. Every prompt whose output is only ever read by another LLM (which is everything except `brief`) can keep using Markdown headings without issue; tags are specifically for the one prompt whose output is parsed by code.

## Detailed Design

### The brief is a semi-structured contract — and needs XML tags, not Markdown headings, to hold

Every `llm_text`-based expansion task (synopsis, press-dossier, story-map, etc.) hands the whole brief to another LLM as free-text context — none of them need to parse it, so any of them can use any tag in whatever way their own prompt asks for. Only `metadata` parses the brief with plain code (no LLM call at all), so exactly one tag is load-bearing and must always appear: `<overview>`, which its running description is built from. Every other tag — including `<locations>`, which is only ever consumed by another LLM call (`story-map`), never parsed by code — is free: a publisher can add, rename, or drop categories to fit their own subject matter (the reference example below adds `<temples>` and `<ethnic_groups>`, appropriate for Central Asian travel narratives and load-bearing for this imprint's `story-map` prompt specifically, not for any engine) without touching any engine code.

**This was originally specified as Markdown headings (`## Overview`, `## Locations`, ...) and had to be changed to XML-style tags after testing exposed a reliability problem specific to this task.** The other multi-section prompts (`one-pager`, `goodreads-profile`, `press-dossier`, `story-map`) all reliably reproduce prescribed Markdown headings once written in the fill-in-the-template style described in Decision, point 6. `marketing_extract_task.txt` was written the same way and still failed: rather than dropping `## Overview`, the model paraphrased it into a descriptive title synthesized from the section's own instructions (e.g. "## Who is narrating, where, when, what is happening") — and inconsistently between chunks of the *same* run, so even hunting for one specific alternate heading wouldn't have helped. Markdown headings visually resemble prose section titles, which apparently invites a model to "improve" a terse one-word heading into something more descriptive when the surrounding instruction is long; XML-style tags don't have that problem, because they read as unambiguously structural, not as content to polish. Switching `<overview>`, `<events>`, `<locations>`, `<temples>`, `<ethnic_groups>`, `<notable_quotes>`, `<tone>` to tags fixed it outright — reproduced exactly, in order, across all chunks of a multi-chunk manuscript, with no further iteration needed. The example extraction prompt (`prompts/examples/s1d/marketing_extract_task.txt`) establishes the one required tag plus a starting set of optional ones:

```
<overview>          <- required (parsed by code, in `metadata`)
(2-4 sentences: who, where, when, why this journey matters)
</overview>

<events>
(chronological bullet list of key events, in order)
</events>

<locations>         <- consumed by the `story-map` prompt, not parsed by code
- Place Name — one-line context (chapter reference if identifiable)
</locations>

<notable_quotes>
(short list, verbatim where possible)
</notable_quotes>

<tone>
(register, voice, period-appropriate texture worth preserving in marketing copy)
</tone>

<temples>           <- optional, imprint-specific; this imprint's story-map uses it
<ethnic_groups>      <- optional, imprint-specific; this imprint's story-map uses it
```

If a publisher rewrites this prompt and drops `<overview>`, `metadata` will find nothing to parse — it treats a missing tag as "write a placeholder and warn," not a crash, since the failure mode is a formatting mismatch in a personal prompt file, not a bug. Dropping `<locations>`/`<temples>`/`<ethnic_groups>` doesn't break anything mechanically — `story-map` just has less material to work with, since it consumes the brief as free text like every other expansion task.

**Chunking note:** the map step runs once per manuscript chunk, and `llm_text` concatenates chunk outputs verbatim — it does not merge or dedupe across chunks. A 3-chunk manuscript therefore produces three complete repetitions of every tag in the brief, in chunk order. This is intentional (it preserves chronological order without asking the LLM to reconcile chunks it never saw together), but it means `story-map` and `metadata` must scan the *entire* brief for every occurrence of their tag, not just the first — confirmed in testing: all three chunks' `<overview>` tags contained real, distinct orienting content (the model never actually used the documented "no new orienting detail" escape hatch), so `metadata` concatenates every `<overview>` occurrence into its description rather than using only the first.

**`max_chars` note:** `llm_text`'s per-call chunk size was hardcoded to 8,000 characters, which is correct for `brief` (it must chunk a long manuscript) but wrong for the five expansion tasks below — they read the (much smaller) brief and must synthesize it in one call, not have it silently re-chunked into several unrelated partial documents if a very long book's brief happens to exceed 8,000 characters. `llm_text.run()` gained an optional `max_chars: int = 8000` parameter (default preserves `cleanup`/`ortho`/`copyedit`/`brief` exactly); each expansion task sets `max_chars: 100000` in its manifest entry, comfortably above any realistic brief size.

### `story-map`'s document shape

Unlike the other expansion tasks, `story-map` produces a fixed six-section reference document (this imprint's actual working format, not a generic template):

1. **Introduction** — outline of the journey, with a note on what makes this edition notable (e.g. first translation into the target language) where applicable.
2. **Biography** — of the author, or of each named member of a multi-person expedition.
3. **Journey Waypoints** — an ordered list of place names as they appear in the source text, each with its modern name in parentheses where the model is confident of one (e.g. "Semipalatinsk (Semey)", "Kalgan (Zhangjiakou)") — omitted rather than guessed if unsure. This is the one part of the pipeline that leans on the LLM's general historical-geography knowledge rather than only the brief, since old toponyms falling out of use is the whole reason this section exists. **Names only, never coordinates** — a wrong modern name is a one-line, human-checkable correction; a hallucinated lat/long is not, so coordinate generation is explicitly out of scope for this task, same conclusion as the original plan, reached for the same reason.
4. **Chronology** — dated or datable key events, in order.
5. **Temples and Ruins** — notable structures, each with enough context for the editor to research further and source period/contemporary photographs.
6. **Ethnic Groups** — notable groups encountered or discussed, same treatment.

The reference prompt leans on the brief's optional `<locations>`/`<temples>`/`<ethnic_groups>` tags where present, but doesn't require them — like every other expansion task, it's free to use whatever the brief contains.

### `lib/metadata_blocks.py`

`templates/marketing_metadata.yaml` (gitignored, per-book; `.example.yaml` committed) groups facts under named keys, each an ordered list of `{label, value}` pairs — labels in any language, order preserved, a blank `value` simply drops that bullet rather than showing an empty or invented one:

```yaml
book_facts:
  - label: "Autor"
    value: "Sven Hedin"
  - label: "ISBN"
    value: "978-1-0686007-2-2"
  # ...

contact_facts:
  - label: "Email"
    value: "hola@ecosdeoriente.com"
  - label: "Trailer"
    value: ""   # not ready yet — bullet omitted, not fabricated
```

`llm_text.run()` takes two independent optional params built on this file:
- `metadata_config: str` — path to the YAML. Loaded (and validated to exist) before any LLM calls are made, so a missing file fails fast rather than after burning tokens. If set, `metadata_blocks.substitute()` replaces any `{{block_name}}` token found *anywhere in the LLM's own output* with that block rendered as Markdown bullets. This is for facts that land at a position only the LLM can determine (e.g. right after a specific paragraph it wrote).
- `metadata_footer: str` — a block name to unconditionally append after the LLM's output, regardless of what the model wrote. This is for facts that always belong at a fixed position (the very end) — seeding it as a `{{...}}` token instead was tried and reliably failed (see Decision, point 5), so this path never asks the model to remember anything.

Any block in the YAML that a given task's prompt doesn't reference is simply unused by that task — one `marketing_metadata.yaml` can feed multiple `s1d` tasks with different needs.

### File layout

```
engines/
  llm_text.py          # unchanged interface, gained metadata_config/metadata_footer params — reused for brief AND all six expansion tasks
  metadata_doc.py        # new — merge marketing_metadata.yaml + brief's Overview

lib/
  metadata_blocks.py     # new — shared load/render/substitute/append_footer for marketing_metadata.yaml

prompts/
  s1d/
    marketing_extract_task.txt     # map step: manuscript chunk -> compact structured extract
    synopsis_task.txt
    press_dossier_task.txt
    one_pager_task.txt
    goodreads_profile_task.txt
    trailer_storyboard_task.txt
    story_map_task.txt
  examples/
    s1d/
      marketing_extract_task.txt
      synopsis_task.txt
      press_dossier_task.txt
      one_pager_task.txt
      goodreads_profile_task.txt
      trailer_storyboard_task.txt
      story_map_task.txt

templates/
  marketing_metadata.example.yaml   # committed
  marketing_metadata.yaml            # gitignored, per-book facts
```

`engines/llm_multi_doc.py` and `prompts/marketing_task.txt` are retired (done) — nothing uses the "one call, split by heading" pattern after this change.

### `systems/s1d/tasks.yaml`

```yaml
tasks:
  - name: brief
    engine: llm_text
    prompt: prompts/s1d/marketing_extract_task.txt
    manifest_key: brief

  - name: synopsis
    engine: llm_text
    prompt: prompts/s1d/synopsis_task.txt
    max_chars: 100000

  - name: press-dossier
    engine: llm_text
    prompt: prompts/s1d/press_dossier_task.txt
    max_chars: 100000
    metadata_config: templates/marketing_metadata.yaml
    metadata_footer: contact_facts   # book_facts is injected via a {{book_facts}} token in the prompt itself

  - name: one-pager
    engine: llm_text
    prompt: prompts/s1d/one_pager_task.txt
    max_chars: 100000

  - name: goodreads-profile
    engine: llm_text
    prompt: prompts/s1d/goodreads_profile_task.txt
    max_chars: 100000

  - name: trailer-storyboard
    engine: llm_text
    prompt: prompts/s1d/trailer_storyboard_task.txt
    max_chars: 100000

  - name: story-map
    engine: llm_text
    prompt: prompts/s1d/story_map_task.txt
    max_chars: 100000

  - name: metadata
    engine: metadata_doc
    metadata_config: templates/marketing_metadata.yaml
```

A publisher who doesn't do press kits deletes four lines. One who wants an extra "blog-post" deliverable adds a prompt file and four lines, same as System 1B — no Python either way.

### CLI usage (explicit chaining, matching System 1B)

```bash
uv run python pipeline.py s1d brief books/test/copyedit/es/zayagan-chp1.txt
uv run python pipeline.py s1d synopsis books/test/brief/es/zayagan-chp1.txt
uv run python pipeline.py s1d press-dossier books/test/brief/es/zayagan-chp1.txt
uv run python pipeline.py s1d one-pager books/test/brief/es/zayagan-chp1.txt
uv run python pipeline.py s1d goodreads-profile books/test/brief/es/zayagan-chp1.txt
uv run python pipeline.py s1d trailer-storyboard books/test/brief/es/zayagan-chp1.txt
uv run python pipeline.py s1d story-map books/test/brief/es/zayagan-chp1.txt
uv run python pipeline.py s1d metadata books/test/brief/es/zayagan-chp1.txt
```

### Consequence for `task_loader.py`: retiring directory-arg tasks

Every remaining System 1D task takes a single file (`CLI_ARG = "file"`, the loader's default) — none needs `CLI_ARG = "directory"` or the `input:`-driven manuscript-discovery fallback chain that only `llm_multi_doc` used. Per the project's standing rule against speculative provisions, `lib/task_loader.py`'s `directory` branch, the `book_dir` argument path, and the unused `input_folder` plumbing are deleted rather than left in place for a hypothetical future engine. If a genuine directory-level task ever appears, that mechanism can be rebuilt against a concrete need.

## Alternatives Considered

(Recap of the three options discussed with the user; Option A below is this decision.)

- **A — Extract once, expand many (chosen).** One full read of the manuscript regardless of how many downstream documents exist; downstream cost and accuracy no longer scale with manuscript length.
- **B — Grouped calls, no brief.** 2-3 calls, each re-sending a truncated manuscript, grouped by register. Simpler but re-pays for the manuscript 2-3x and keeps the truncation-accuracy defect for anything needing the full plot arc.
- **C — One call per deliverable.** Maximum per-document prompt specialization, worst cost (7x manuscript re-send). Rejected — five of the seven deliverables share a marketing register closely enough that specialization doesn't clearly need seven independent full-manuscript reads.

## Consequences

**Easier:**
- Adding, removing, or reordering marketing deliverables is a `tasks.yaml` edit plus a prompt file — identical mechanism to System 1B, satisfying "other imprints may do more or less than me."
- Each deliverable has its own prompt, independently tunable, without touching the others.
- Manuscript length stops being a hidden accuracy risk — the brief step reads all of it, once, regardless of book length.
- `metadata` runs at zero LLM cost, the one deliverable that's actually pure data merging.
- `lib/metadata_blocks.py` means any future `s1d` task can inject bibliographic/contact facts without inventing a new mechanism — `press-dossier` was the first to need it, not the last that's expected to.

**Harder / needs care:**
- `metadata` depends on the extraction prompt honoring one tag (`<overview>`). This is real coupling between a gitignored, user-editable prompt and one engine's parsing logic — documented above, and the engine must degrade to a clear warning rather than a crash if the tag is missing.
- `story-map`'s modern-toponym lookup relies on the model's general knowledge, not the brief or any verified gazetteer — it will occasionally be wrong or decline to know one, which is why the prompt is instructed to omit rather than guess. Treat every parenthetical as a claim to spot-check, not a fact.
- A metadata block's position determines which mechanism it needs: content-dependent placement can use a `{{token}}` the LLM places itself (reliable, tested); a fixed trailing position must use `metadata_footer` instead (a trailing `{{token}}` instruction was tried and reliably ignored by the model — see Decision, point 5). Get this backwards and the block silently never appears, with no error, since the engine can't distinguish "block correctly omitted" from "model forgot."
- Any new multi-section prompt should be written as a literal fill-in-the-template from the start (headings on their own line, instructions inside `[brackets]` beneath them, an explicit "these headings are literal" rule) — see Decision, point 6. Writing "`## Heading` — prose describing the section" and expecting the heading to be reproduced verbatim is the mistake that had to be found and fixed four times before it was written down here.
- For a prompt whose output another task parses with code (`marketing_extract_task.txt` is the one example so far), prefer XML-style tags over Markdown headings from the start, even after applying the fill-in-the-template fix above — see Decision, point 7. Markdown headings failed there in a way none of the fixed-heading document prompts did (paraphrased into a different, inconsistent title per chunk, not just dropped), and tags fixed it immediately with no further iteration. Markdown headings remain fine for prompts nothing parses — every purely LLM-to-LLM document in this pipeline still uses them without issue.
- Even after the point-6 template fix, `press-dossier` produced one further heading defect in a later regression run: every prescribed heading came out with a spurious extra marker prepended (`# ## Biografía del autor`, `### ### La puerta norte...`) — a fourth distinct way this class of bug has surfaced, at non-zero temperature (0.4), non-deterministically (clean on some runs, corrupted on others). Rather than attempt a fifth prompt-wording iteration against a moving, low-frequency target, this one was fixed in code: `engines/llm_text.py` now unconditionally strips a leading `#{1,6} ` when it's immediately followed by another `#{1,6} ` on the same line, applied to every `llm_text` task's output. Safe to apply universally — no legitimate line ever starts with two consecutive heading markers, and `cleanup`/`ortho`/`copyedit` (which don't use Markdown headings at all) are unaffected. General lesson: once a formatting defect is precisely, mechanically describable, normalize it in code rather than continuing to iterate on prompt wording against non-deterministic model behavior — the same judgment call already made for `metadata_footer` (Decision, point 5).
- Very long manuscripts still mean many chunk calls in the `brief` step (one per ~8,000 characters) — each call is cheap, but call *count* (latency, rate limits) scales with book length. Not a cost problem; worth revisiting only if it becomes a latency complaint.
- `run-all` for a system remains out of scope (already deferred for System 1B) — each of the eight commands above is still run by hand, in order, per book.

## Migration Checklist

- [x] Write `prompts/examples/s1d/marketing_extract_task.txt` (English reference) establishing the heading contract above
- [x] Rewrite `systems/s1d/tasks.yaml` with the `brief` task
- [x] Delete `engines/llm_multi_doc.py` and `prompts/marketing_task.txt` / `prompts/examples/marketing_task.txt`
- [x] Remove the `directory` `CLI_ARG` branch, `book_dir` handling, and `input:` manuscript-discovery fallback from `lib/task_loader.py`
- [x] Reorganize `prompts/` into one folder per VSM system (`prompts/s1b/`, `prompts/s1d/`, mirrored under `prompts/examples/`) — not originally scoped in this ADR, added once System 1D's prompt count made a flat `prompts/` folder unworkable; `.gitignore` updated to `/prompts/*/*_task.txt`
- [x] End-to-end test of `brief` against `books/test/` — confirmed working, including the expected behaviour that a multi-chunk manuscript produces one full heading-set per chunk in the brief (no merge/dedupe across chunks)
- [x] Add optional `max_chars` param to `engines/llm_text.py` (default 8000, unchanged for existing tasks) so expansion tasks can read a whole brief in one call instead of being re-chunked
- [x] Write `prompts/examples/s1d/synopsis_task.txt` (English reference) and add the `synopsis` task to `systems/s1d/tasks.yaml` (`max_chars: 100000`)
- [x] Add optional per-call `temperature` override (`providers/llm/base.py`, `providers/llm/mistral.py`, `engines/llm_text.py`), default preserves existing 0.0 behavior; `synopsis` sets `temperature: 0.7` since marketing prose benefits from more variation than fidelity-critical editorial passes
- [x] Revise this ADR: `story-map` moves from a planned code-only engine to an `llm_text` task — the real requirement is a six-section document (introduction, biography, waypoints with modern-toponym lookup, chronology, temples/ruins, ethnic groups), which needs LLM synthesis and gazetteer knowledge, not just parsing; coordinate generation remains explicitly out of scope
- [x] Write `prompts/examples/s1d/story_map_task.txt` (English reference) and add the `story-map` task to `systems/s1d/tasks.yaml`
- [x] Write `prompts/examples/s1d/one_pager_task.txt` (English reference) and add the `one-pager` task to `systems/s1d/tasks.yaml` (`temperature: 0.4`)
- [x] Fix code-fence-wrapping (models occasionally wrapped output in ` ```markdown `): added an explicit "no code block" rule to `one_pager`, `synopsis`, and `story_map` prompts (real + example)
- [x] Add `lib/metadata_blocks.py` (load/render/substitute/append_footer) and `templates/marketing_metadata.example.yaml` (committed); `.gitignore` updated to add `/templates/marketing_metadata.yaml`
- [x] Add `metadata_config`/`metadata_footer` params to `engines/llm_text.py`; validated up front (before any LLM calls) so a missing config fails fast
- [x] Write `prompts/examples/s1d/press_dossier_task.txt` (English reference, modeled on this imprint's real dosier de prensa PDF) and add the `press-dossier` task to `systems/s1d/tasks.yaml` (`metadata_config` + `metadata_footer: contact_facts`)
- [x] Write `prompts/examples/s1d/trailer_storyboard_task.txt` (English reference) and add the `trailer-storyboard` task to `systems/s1d/tasks.yaml` (`temperature: 0.6`). Two storyboards (60s/90s) generated per run, sharing one brief read. Found in testing that the model's cumulative scene timestamps don't land exactly on 60/90 seconds (off by several seconds) — accepted as approximate per user decision, since this is a manual video-editing planning aid, not a precision deliverable; prompt now instructs the model to say so explicitly in the output rather than implying false precision.
- [x] Write `prompts/examples/s1d/goodreads_profile_task.txt` (English reference) and add the `goodreads-profile` task to `systems/s1d/tasks.yaml` (`temperature: 0.4`)
- [x] Fix heading fidelity: rewrote `one_pager`, `goodreads_profile`, `press_dossier`, and `story_map` prompts (real + example) as explicit fill-in-the-template structures — literal headings on their own line, instructions moved into `[bracketed placeholders]` beneath them. `press_dossier` additionally needed its historical-context heading fixed (`## El corazón de la historia`) instead of model-invented, with creative variation preserved as an italic subtitle instead — see Decision, point 6.
- [x] Fixed the extraction contract from Markdown headings to XML tags (`<overview>`, `<events>`, `<locations>`, `<temples>`, `<ethnic_groups>`, `<notable_quotes>`, `<tone>`) in `marketing_extract_task.txt` (real + example) after the point-6 fill-in-the-template fix didn't hold for this one prompt — see Decision, point 7. Regenerated `books/test/brief/` against the corrected prompt; confirmed all 21 tags (7 × 3 chunks) reproduced exactly.
- [x] Add `engines/metadata_doc.py` — read `marketing_metadata.yaml` + every `<overview>` tag in the brief (concatenated, not just the first — all three test chunks had real content, not the "no new orienting detail" placeholder), write `metadata.md`; warns (doesn't crash) if no `<overview>` tag is found. Reuses `lib/metadata_blocks.py`'s `load()`/`render_block()`; renders every top-level block in `marketing_metadata.yaml` generically (heading auto-derived from the YAML key), not just `book_facts`/`contact_facts` by name.
- [x] Add the `metadata` task to `systems/s1d/tasks.yaml` (`engine: metadata_doc`, no `max_chars`/`temperature` — not applicable to a non-LLM task)
- [x] End-to-end regression test of all eight commands against `books/test/` after the `brief` tag-format change — found and fixed one further heading-corruption variant in `press-dossier` (normalized in `engines/llm_text.py` rather than a fifth prompt iteration, see Decision addendum above); re-verified all eight outputs clean (no code fences, no doubled headings)
- [x] Update README's System 1D section and command examples: architecture layer list, task reference table (all eight tasks), `templates/` section split into `format_styles.yaml`/`marketing_metadata.yaml`, book folder tree, Setup steps 4-6 (prompts + metadata config), and the Running the Pipeline walkthrough
