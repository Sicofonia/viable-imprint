# ADR 006 — System 5: Editorial Policy Agent (Candidate Scope Evaluation)

**Status:** Implemented. Built and tested end-to-end against real Mistral calls (one clearly in-scope candidate, one clearly out-of-scope) — see "Implementation notes" for two prompt-reliability findings, both fixed by prompt wording, no code normalizers needed.

---

## Context

The private strategic assessment's recommended sequence, after System 2 (ADR 004) and System 3 (ADR 005), is: *"Implement the S5 policy agent (it was in the original sketch and is nearly free: the `vsm.md` policy as a system prompt) and route S1A candidate evaluation through it."* The assessment separately named this as one of the project's untested, judgment-heavy gaps: *"S5 arbitration ('does this borderline text fit our scope?') appeared in the initial sketch as a token policy agent and was never implemented."*

Discussing this ADR, a second question came up first: Beer's System 5 also arbitrates conflicts between System 3 (present-day operational efficiency) and System 4 (future adaptation) when their demands compete for resources — `vsm.md` states this explicitly ("System 5... arbitrates conflicts between System 3... and System 4... when their demands are contradictory"). That's a real, distinct S5 responsibility, but a different one: a resourcing tradeoff, fed by System 3's metrics (ADR 005) and System 4's briefings (ADR 003), not a candidate-text description. **The user explicitly asked to scope this ADR to the first aspect only** — S5 as the editorial-policy arbiter for candidate evaluation — and to defer the System 3/4 conflict-arbitration role to a later ADR, to keep this one minimal.

`docs/vsm.md` already has the substance fully written out, twice over:
- **System 5's editorial policy** — thematic scope (what the imprint publishes / does not publish), non-negotiable values (source rigour, historiographical honesty, quality of the book as object, accessibility) — and its stated arbitration job: *"does a text fall within the imprint's scope? Is the source found sufficiently reliable? Does the commissioned introduction have the appropriate perspective?"*
- **System 1A's responsibilities** — identifying and assessing candidate texts, deciding on language/translation, defining the critical apparatus — none of which exist in code today. Checked directly: no `systems/s1a/`, no S1A prompts, no S1A docs anywhere in the repo.

This ADR does not build System 1A. It builds the smallest possible front door that lets a human-written candidate description reach System 5's policy judgment, and nothing past that.

---

## Decision

### 1. Scope: an advisory verdict, not a gate

`s5 evaluate` reads a human-written candidate description and returns a structured recommendation against the editorial policy — fits / borderline / doesn't fit, with reasoning tied to specific policy criteria. It does **not**: maintain a candidates database, automatically create a book folder, block or permit anything downstream, or touch the System 3/4 resourcing-conflict role (deferred). The Editorial Director reads the verdict and decides; if they proceed, they run `pipeline.py init <slug>` themselves, same deliberate manual act as always. This matches the assessment's own framing of the achievable near-term target — *"human compressed to System 5 plus sporadic audit"* — not human replaced.

### 2. Reuse `llm_text` wholesale — the "policy agent" is a prompt, not new code

Consistent with the assessment's "nearly free" framing: `engines/llm_text.py` is unchanged. The policy agent is `vsm.md`'s System 5 section, reformatted as a system prompt, run through the exact same chunk-and-complete mechanism that already powers eleven tasks across three systems. `systems/s5/tasks.yaml` gains one entry:

```yaml
tasks:
  - name: evaluate
    engine: llm_text
    prompt: prompts/s5/policy_evaluation_task.txt
    max_chars: 100000   # a candidate brief is short prose, read in one call, not chunked
```

`pipeline.py`'s existing `build_system_group("s5", ...)` registration is the only new line in `pipeline.py` itself for the CLI side — the same dynamic-manifest mechanism that already builds `s1b`/`s1d`/`s4`.

### 3. Candidates aren't book-scoped — a new perpetual `candidates/` root, same shape as `intelligence/`

A text under evaluation isn't a book yet — there's no editorial commitment to acquire it, so it has no business under `books/`. This is the same situation ADR 003 solved for System 4: a perpetual project folder, structured like a book internally (its own `manifest.yaml`) purely so the existing `llm_text`/`paths.book_root()` machinery works unmodified.

```
candidates/
├── manifest.yaml
├── s1a/
│   └── briefs/
│       └── <candidate-slug>.txt      # human-written, dropped in by hand
└── s5/
    └── evaluate/
        └── <candidate-slug>.txt      # the verdict
```

`s1a/briefs/` holds the input; `s5/evaluate/` holds System 5's output — matching the established "output nests under the *producing* system" rule from ADR 001's amendment (`s5 evaluate` writes under `s5/` even though its input came from `s1a/`, exactly like `s1d brief` writes under `s1d/` while reading from `s1b/`).

**This is a folder-naming convention, not an implementation of System 1A.** Nothing in this ADR identifies, sources, or assesses a candidate — a human still does every part of that job, exactly as they do today, just writing the result into a file instead of carrying it in their head. `s1a/briefs/` is named that way only because it's *whichever* system's operational output lands there conceptually, mirroring how every other folder in this project already names itself after the VSM system that produces its contents. See point 8 for the explicit line this draws.

### 4. Minimal bootstrap: one new top-level command, mirroring `init`

`paths.py` gains `candidates_root(config)`, copied from `intelligence_root()`'s exact shape — creates `candidates/` and its `manifest.yaml` on first use:

```python
def candidates_root(config: dict) -> Path:
    root = Path(config.get("candidates_dir", "candidates")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.yaml"
    if not manifest_path.exists():
        manifest_path.write_text("slug: candidates\n", encoding="utf-8")
    return root
```

A new hand-written `pipeline.py candidate new <slug>` command (parallel to `init`, not manifest-driven — mirrors System 2/3's precedent of a small hand-written group for something that isn't itself a `tasks.yaml`-declared task) creates `candidates/s1a/briefs/` and calls `candidates_root()`:

```python
@click.group(name="candidate")
def candidate():
    pass

@candidate.command(name="new")
@click.argument("candidate_slug")
@click.pass_context
def candidate_new(ctx, candidate_slug):
    root = paths.candidates_root(ctx.obj["config"])
    (root / "s1a" / "briefs").mkdir(parents=True, exist_ok=True)
    click.echo(f"Write the candidate's description in: {root / 's1a' / 'briefs' / (candidate_slug + '.txt')}")
    click.echo(f"Then run: pipeline.py s5 evaluate {root / 's1a' / 'briefs' / (candidate_slug + '.txt')}")
```

This is the only genuinely new Python this ADR needs — everything else is a `tasks.yaml` entry and a prompt file. **`candidate new` does no S1A work** — it creates an empty folder and a manifest, the identical move `init` already makes for books (`init` doesn't perform System 1B's cleanup/translation/copyediting either; it just makes room for a manuscript). It doesn't search anything, doesn't propose anything, doesn't decide anything is a candidate — it's scaffolding for wherever a candidate brief lands, however that brief actually got written.

### 5. The candidate brief stays free-form prose — no rigid schema

Matching every other human-authored input in this project (manuscripts, `marketing_metadata.yaml`'s facts), a candidate brief is plain prose, not a rigid form. The prompt lists what's useful to include (title, author, approximate date, language, a paragraph on subject/scope, what's known about copyright status, notes on source quality/availability) as guidance, not a required schema — nothing parses this file with code, so there's nothing to break by writing it loosely.

### 6. Verdict structure: fill-in-template Markdown, no XML tags

This project's established reliability lessons (ADR 002 points 6–7, ADR 003 point 10) distinguish two cases: a prompt whose output is parsed by *code* needs XML-style tags; a prompt whose output is read only by a *human* (like `s4 briefing`) can use ordinary Markdown headings, written as an explicit fill-in-the-template (literal headings, instructions in `[bracketed placeholders]` beneath them, a rule that headings are reproduced verbatim). `s5 evaluate`'s verdict is human-read only — nothing downstream parses it — so it follows the `s4 briefing` pattern, not the `brief` pattern:

```
## Veredicto
[Encaja / Caso límite / No encaja]

## Ámbito temático
[How the candidate measures against each explicit in/out criterion from vsm.md's thematic scope — period, geography, public-domain status, first-person travel/exploration component]

## Valores no negociables
[Any concerns against source rigour, historiographical honesty, quality as object, accessibility — or "sin objeciones" if none]

## Recomendación
[A plain-language recommendation, and — for a borderline case — what specific fact would resolve it]
```

If a future need for machine-parsing arises (e.g. a candidates dashboard), XML-tagging the verdict line then is the same reactive move ADR 002 made for `brief` — not done speculatively now.

### 7. The prompt is derived from `vsm.md`, and — unlike other prompts — is committed, not gitignored

Every other task prompt (`prompts/s1b/`, `prompts/s1d/`, `prompts/s4/`) is gitignored because it encodes a specific imprint's editorial voice — content the committed repo shouldn't presume to know. System 5's prompt is different: it's substantively *the same content already sitting, in full, in `docs/vsm.md`* — a committed, public document (this imprint's actual mission, real author list, real thematic scope, real non-negotiable values, all already in the repo). Gitignoring a second copy of already-public content wouldn't protect anything and would just be inconsistent. `prompts/s5/policy_evaluation_task.txt` is therefore **committed**, with `.gitignore`'s existing `/prompts/*/*_task.txt` rule given one explicit exception:

```
/prompts/*/*_task.txt
!/prompts/s5/policy_evaluation_task.txt
```

`prompts/examples/s5/` still exists alongside it, genericized, for a different publisher who needs their own placeholder policy to start from — same reasoning as every other `examples/` folder, just with the real file *also* committed here specifically, which is new.

**Sync risk, accepted for now:** the prompt is a separate, hand-authored file, not code that reads `vsm.md` at runtime — dynamically extracting a Markdown section was considered and is discussed in Alternatives. `vsm.md` itself says its editorial policy is "revised annually," so this file needs a human to remember to update it when that happens. Mitigated only by a comment at the top of the prompt file pointing back at `vsm.md`'s System 5 section and this ADR — a documentation safeguard, not a code one.

### 8. System 1A is not built by this ADR — draw this line explicitly, not just by omission

Worth stating plainly rather than leaving implicit: **this ADR automates System 5's job (the adequacy check) and nothing of System 1A's job (coming up with the candidate in the first place).** `vsm.md`'s System 1A responsibilities — identify candidate texts, verify legal status, locate the best available source, assess quality and completeness — are entirely unaddressed here. A human still does all of that, unaided, exactly as today. The only thing that changes is where the result of that human work gets written down (a file, instead of nowhere) and that something now checks it against policy automatically.

Two things follow from taking this seriously, not just noting it:

- **No `pipeline.py s1a` CLI group, no `systems/s1a/tasks.yaml`.** System 1A has exactly one "action" in this ADR's scope — a human writing a text file — which isn't an automated task at all, so per the existing rule ("only systems with implemented tasks exist" as CLI groups) there's nothing to register. Its only footprint is the `s1a/briefs/` folder convention under `candidates/` — a place for output, not a system that produces it.
- **This is the first VSM-named folder in this project with no engine behind it.** Every other `s1b/`, `s1d/`, `s4/` folder corresponds to real, automated work — a task in a `tasks.yaml`, an engine, a prompt. `candidates/s1a/` breaks that pattern on purpose: it's named for *conceptual* provenance (this is where System 1A's output would live, whoever or whatever produces it), not because System 1A has been implemented. A future reader skimming folder names alongside `s1b/`/`s1d/`/`s4/` could reasonably assume otherwise — this note exists so they don't.

Building out real System 1A automation — one that actually proposes candidates rather than waiting for a human to bring one — is separate, larger, explicitly out-of-scope work. If it happens, System 4's Gutenberg-catalog scan (subject-keyword matching against new public-domain listings) is the closest thing already built to a starting point, since it's already doing a slice of "periodic review of public-domain digital sources for new finds" — just filed under System 4 today rather than System 1A.

---

## Alternatives Considered

- **Also automating the System 3/4 resourcing-conflict arbitration in this ADR** — rejected per the user's explicit direction: different question shape (a resourcing tradeoff, not a scope-fit judgment), different inputs (S3 dashboard + S4 briefing, not a candidate description). Left for a future ADR.
- **Dynamically extracting System 5's section from `docs/vsm.md` at runtime instead of a hand-authored prompt file** — genuinely considered, not just dismissed: it would eliminate the sync-drift risk in point 7 entirely, and section extraction from a heading-delimited Markdown file is a much more tractable, deterministic problem than the LLM-output-parsing fragility this project's ADRs usually warn about. Deferred anyway, for now: it breaks the pattern every other prompt in this project follows (a standalone, hand-tunable file — the prompt needs LLM-oriented instructions and formatting distinct from `vsm.md`'s human-oriented prose, not a verbatim re-serving of it), and adds a code path to solve a problem (policy drift) that hasn't actually happened yet. Worth revisiting if the hand-copy approach is ever caught actually drifting out of sync.
- **Building a real System 1A (candidates database, priority queue) alongside this ADR** — rejected, explicitly out of scope per the user's "keep scope minimal" direction; also a meaningfully larger, separable piece of work than "give S5 something to evaluate."
- **Reusing `books/`/`init` for candidates under evaluation** — rejected: conflates "under evaluation, no commitment yet" with "in production," and would litter `books_dir` with rejected candidates. `candidates/`, mirroring `intelligence/`'s precedent, keeps the distinction clean.
- **Letting a "fits" verdict automatically create the book folder** — rejected: collapses an advisory recommendation into an autonomous decision. The assessment's own framing keeps System 5 arbitration as something a human still acts on, not something that acts by itself.
- **XML-tagging the verdict for future machine-parseability** — rejected for now, same reasoning as `s4 briefing`: nothing parses this today; tags get introduced reactively if that changes (ADR 002 point 7's precedent), not spec­ulatively.
- **A dedicated `pipeline.py s1a` CLI group** — rejected: no automated S1A task exists in this scope; an empty command group would be dead weight against the project's own "only systems with implemented tasks exist" rule.

---

## Consequences

**Easier:**
- A borderline "does this fit our scope" question gets a first-pass, policy-grounded written opinion in one command, instead of the Editorial Director re-deriving the reasoning from `vsm.md` from memory each time.
- Zero new engine code — `llm_text.py` is untouched; this is a prompt file plus a `tasks.yaml` entry plus one small bootstrap command, matching the assessment's "nearly free" framing almost exactly.
- Gives future System 1A automation a place to land its output (`candidates/s1a/briefs/`) without having to build or even design that automation now — but see point 8: this is a landing spot, not a head start on System 1A itself, and shouldn't be counted as partial progress on it.

**Harder / needs care:**
- The verdict is a judgment call from a model, not a determination — false negatives (rejecting a text that actually fits) and false positives (recommending a text that doesn't) are both possible and expected; this is a first-pass opinion for the Editorial Director to weigh, not a filter to trust unattended.
- The prompt is a hand-maintained copy of `vsm.md`'s policy, not a live read of it — if the annual policy revision `vsm.md` describes happens and nobody remembers to update `prompts/s5/policy_evaluation_task.txt`, the agent silently arbitrates against a stale policy. No code catches this; only the header-comment cross-reference (point 7) and this ADR's existence make it findable.
- `candidates/` is a fourth top-level gitignored data folder (after `books/`, `intelligence/`, and the personal `templates`/`prompts` files) — worth remembering it exists when reasoning about "where does this project keep state," same caveat System 4's `intelligence/` already introduced.
- This ADR deliberately leaves the System 3/4 conflict-arbitration half of System 5 unbuilt — `vsm.md`'s System 5 section will read as only partially implemented until that follow-up ADR happens.
- `candidates/s1a/` looking like every other VSM-named, engine-backed folder in this project is a real readability risk (point 8) — anyone skimming the repo's structure later could mistake it for evidence System 1A is implemented. Worth keeping this ADR's point 8 note intact rather than trimming it as "obvious" in a future edit.

---

## Implementation notes (2026-07-10)

Built end-to-end and tested against real Mistral calls (not simulated) on a disposable pair of test candidates — one clearly in-scope (a real Sven Hedin title, 1898, Pamir/Central Asia, confirmed public domain), one clearly out-of-scope (a fictional 2019 travel memoir set in Provence, still in copyright) — created and deleted within this session, not part of the repo. Two prompt-reliability findings, both from this project's already-known failure family (ADR 002 points 6–7, ADR 003 point 10), both fixed by prompt wording alone:

### 9. The candidate brief's own `Etiqueta: valor` formatting primed the model to drop the `##` heading prefix entirely

First real run: the model returned the four sections in the right order, with recognizable content, but headed each one as `Veredicto:`, `Evaluación de criterios:`, `Preocupaciones:`, `Recomendación:` — plain labels with a trailing colon, no Markdown `##` at all. The likely cause, consistent with ADR 003 point 10's finding that `scan`'s own `=== SOURCE LABEL ===` delimiters primed the model to invent similarly-styled headings in `briefing`'s output: the test candidate brief was written with its own `Título:`, `Autor:`, `Año:` colon-labeled fields, and the model's response echoed that same input formatting convention instead of switching to the requested Markdown template. Since candidate briefs are deliberately free-form prose (Decision point 5) — a real user's brief could easily use this same labeled-field style — this isn't a one-off test artifact; it's a real, likely-recurring failure mode for this specific task.

Fixed by adding an explicit rule naming the exact failure: *"Cada encabezado debe empezar literalmente con '## '... La descripción del candidato que recibas puede venir con sus propios campos en formato 'Etiqueta: valor'... ignora ese estilo al escribir tu respuesta."* Re-running confirmed all four headings came back with the correct `## ` prefix.

### 10. Even with the `##` prefix fixed, the two middle headings were still paraphrased — the exact ADR 002 point 6 failure, recurring in a new prompt

Second run, `##` prefix now present, but `## Ámbito temático` came back as `## Evaluación de criterios` and `## Valores no negociables` came back as `## Preocupaciones` — the first and last headings (`## Veredicto`, `## Recomendación`) were untouched. This matches ADR 002 point 6's original finding precisely: a model will reliably reproduce a heading shown literally in isolation, but will still paraphrase a heading into something reflecting its own bracketed instruction text when that instruction is long/descriptive — apparently more likely for interior headings than the first/last ones in a sequence, though this prompt's sample size (two runs) isn't enough to call that part a confirmed pattern, just a repeated observation worth watching for.

Fixed the same way ADR 002 eventually did: stopped relying on "reproduce the template above" alone and added a second, redundant rule that lists the four required heading strings verbatim as a flat checklist, explicitly naming the exact wrong outputs just observed as counter-examples (*"nunca variantes como '## Evaluación de criterios' en lugar de '## Ámbito temático'"*). Re-running confirmed all four headings matched exactly, and this held on the second (out-of-scope) test candidate too, with no further iteration needed.

Both findings reinforce, rather than add to, this project's existing prompt-reliability lessons — logged there rather than as new categories. See `feedback-llm-prompt-reliability` memory.

---

## Implementation Checklist

- [x] Add `candidates_dir: candidates` to `config.example.yaml` (and the user's real `config.yaml`), mirroring `intelligence_dir`
- [x] Add `paths.candidates_root(config)` to `lib/paths.py`, mirroring `intelligence_root()`
- [x] Add the hand-written `candidate` command group to `pipeline.py`: `candidate new <slug>` (creates `candidates/s1a/briefs/` + `candidates/manifest.yaml`)
- [x] Create `systems/s5/tasks.yaml` (`evaluate` → `engine: llm_text`, `max_chars: 100000`)
- [x] Register `s5` in `pipeline.py` via the existing `build_system_group("s5", "System 5 — Identity, Values and Policy")` call
- [x] Write `prompts/s5/policy_evaluation_task.txt` (real, Spanish, matching every other real prompt's language) — System 5's mission, thematic scope in/out, and non-negotiable values from `docs/vsm.md`, reformatted as fill-in-template instructions per point 6; header comment cross-referencing `docs/vsm.md`'s System 5 section and this ADR; strengthened twice more during testing, see points 9–10
- [x] Write `prompts/examples/s5/policy_evaluation_task.txt` (English, genericized policy — a placeholder scope/values, not this imprint's real ones, same treatment as every other committed example; headings genericized to English too, matching the `prompts/examples/s1d/` precedent rather than `prompts/examples/s4/`'s Spanish-headings-kept approach)
- [x] Add the `!/prompts/s5/policy_evaluation_task.txt` exception to `.gitignore`, immediately after the existing `/prompts/*/*_task.txt` rule
- [x] Add `/candidates/` to `.gitignore`, alongside `/books/` and `/intelligence/`
- [x] End-to-end test: `candidate new` a test slug, write a short candidate brief (one clear in-scope case, one clear out-of-scope case), run `s5 evaluate`, confirm the verdict structure holds and the reasoning actually engages with the specific policy criteria rather than generic praise/rejection — found and fixed the point-9 and point-10 prompt issues in this pass; both test candidates deleted afterward, not committed
- [x] Update README with the System 5 section and command reference (Architecture, `prompts/` exception note, a new `candidates/` folder-structure block, the task table, and a new usage walkthrough in Running the Pipeline)
