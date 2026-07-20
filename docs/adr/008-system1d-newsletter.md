# ADR 008 — System 1D: Monthly Newsletter (Explorer & Dish of the Month)

**Status:** Proposed. Not yet implemented — for review before work starts.

---

## Context

Raised directly by the user: producing a monthly imprint newsletter — imprint news, an "explorer of the month" spotlight, and an "Asian dish of the month" cultural feature — currently consumes real manual time, and is a recurring task for this imprint (and, plausibly, others this project's audience runs). `docs/vsm.md` already names this under System 1D without needing to stretch the framework: its responsibilities list *"communication and marketing for each title (social media, newsletter, specialist communities)"* and its recurring tasks include *"publication on social media and newsletter around the launch."*

**But structurally, it doesn't belong next to `brief`/`synopsis`/`one-pager`.** Every System 1D task built so far (ADR 002) is per-book, chained from one title's `brief`. A monthly imprint newsletter is calendar-driven and catalog-wide — the same shape System 4's `scan`/`briefing` and ADR 007's homeostat pipeline already are, not the shape the rest of System 1D is. So: **System 1D owns it conceptually; the code needs a fifth perpetual root** (`newsletter/`, after `books/`, `intelligence/`, `candidates/`, `homeostat/`), not a slot inside a book's folder.

Two things were settled with the user before drafting:
- **News sources:** both automatic (recently-completed books, read from the existing S2/S3 ledger) and a short human-written monthly notes file for anything the pipeline can't know on its own (a press mention, a distribution milestone) — mirroring the candidate-brief pattern from ADR 006.
- **Output format:** Markdown only for v1, matching every other System 1D/System 4 synthesis output — not HTML, not email-ready. Copy-paste into whatever's actually used to send it, same as `goodreads-profile` already works.

The crucial constraint, named explicitly by the user: **the newsletter must not repeat an explorer or a dish already featured in a previous issue.**

---

## Decision

### 1. Three pipeline stages, mirroring the shape ADR 007 just established for homeostat

Gather (code) → synthesize (`llm_text`, reused) → a code-only follow-up. Same three-stage shape, same reasoning: the follow-up stage needs deterministic logic (checking and updating tracking files) that must never be delegated to a model.

- **`s1d newsletter-scan`** (new engine, code only, `CLI_ARG = "none"`) — gathers books that completed at least one task within the current calendar month (via `lib.dashboard.portfolio_summary()`, reusing ADR 005's already-structured data — no new ledger-reading logic), this month's notes file if one exists, and both exclusion lists (point 3). Writes a tag-delimited `combined.txt`, same `<fuente>`-style convention `feed_scan.py` established.
- **`s1d newsletter`** (reuses `engines/llm_text.py` completely unchanged) — reads that file, writes the newsletter draft. `max_chars: 100000` (never manuscript-length input, same as every other expansion task).
- **`s1d newsletter-track`** (new engine, code only) — reads the draft, extracts which explorer and dish were featured, updates the two tracking files, and produces the **final, tag-stripped copy** — the actual deliverable, ready to copy-paste. See points 3–4.

```yaml
# systems/s1d/tasks.yaml — additions
  - name: newsletter-scan
    engine: newsletter_scan
    book_scoped: false   # periodic, catalog-wide — not part of any book's task graph; see point 6

  - name: newsletter
    engine: llm_text
    prompt: prompts/s1d/newsletter_task.txt
    max_chars: 100000
    input: s1d.newsletter-scan
    book_scoped: false
    # Real contact facts appended without ever asking the LLM to guess them —
    # same established mechanism press-dossier already uses.
    metadata_config: templates/marketing_metadata.yaml
    metadata_footer: contact_facts

  - name: newsletter-track
    engine: newsletter_track
    input: s1d.newsletter
    book_scoped: false
```

### 2. `newsletter/` — a fifth perpetual root, same shape as the other four

```
newsletter/
├── manifest.yaml
├── featured_explorers.yaml        # flat list: [{name, date_featured}]
├── featured_dishes.yaml           # flat list: [{name, date_featured}]
├── notes/
│   └── 2026-08.txt                # optional, human-written monthly notes
└── s1d/
    ├── newsletter-scan/2026-08-01/combined.txt
    ├── newsletter/2026-08-01/combined.txt        # draft, tags still in place
    └── newsletter-track/2026-08-01/combined.txt  # final, clean copy
```

`lib/paths.py` gains `newsletter_root(config)` — a fourth copy of `intelligence_root()`'s exact pattern. No bootstrap command needed for the notes file, unlike `candidate new`: `newsletter-scan` creates `notes/` itself on first run and, if this month's file doesn't exist yet, warns and continues rather than failing — the newsletter still gets written, just without anything the pipeline couldn't know on its own, same graceful-degradation philosophy `feed_scan.py` already uses for a single failed source.

### 3. Non-repetition — prevention *and* verification, not prevention alone

A model will occasionally ignore an exclusion instruction — this project has hit that exact failure class enough times to not trust an instruction alone here. Two layers:

- **Prevention:** `newsletter-scan` includes both `featured_explorers.yaml` and `featured_dishes.yaml` in the prompt's input, tagged `<exploradores_ya_destacados>`/`<platos_ya_destacados>`, with an explicit instruction not to repeat anything listed — the same mechanism `s4 briefing` already uses to avoid reporting something already in the imprint's own catalog as news (ADR 003's `catalog_reference` block).
- **Verification:** the explorer's and dish's names must come back in a code-parseable tag, not buried in prose — `<explorer>Name</explorer>`, `<dish>Name</dish>` — mirroring `metadata_doc.py`'s existing `_OVERVIEW_RE` extraction pattern exactly. `newsletter-track` checks each extracted name (case-insensitive) against its tracking file. A repeat gets a **warning printed to the console and left in the final output as a visible note**, not a hard failure or a retry loop — this is advisory, reviewed by the Director before sending, and a repeated explorer is a minor embarrassment, not a real defect worth building a retry mechanism for.

```python
# engines/newsletter_track.py (sketch)
_EXPLORER_RE = re.compile(r"<explorer>\s*(.*?)\s*</explorer>", re.DOTALL)
_DISH_RE = re.compile(r"<dish>\s*(.*?)\s*</dish>", re.DOTALL)

def run(input_file, root, system, output_name, config, **params):
    text = input_file.read_text(encoding="utf-8")
    explorer = _extract(_EXPLORER_RE, text)
    dish = _extract(_DISH_RE, text)
    warnings = []
    warnings += _check_and_record(explorer, root / "featured_explorers.yaml", "explorer")
    warnings += _check_and_record(dish, root / "featured_dishes.yaml", "dish")
    clean_text = _EXPLORER_RE.sub(lambda m: m.group(1), text)  # keep the name, drop the tag
    clean_text = _DISH_RE.sub(lambda m: m.group(1), clean_text)
    ...
```

**Known, accepted limit:** matching is exact (case-insensitive) — "Sven Hedin" and "Sven Anders Hedin" would not be recognized as the same person. Not solved here; a fuzzy-matching dependency for two tracking lists is disproportionate. Also worth naming plainly: the pool of on-brand explorers is large but not infinite (`vsm.md` names nine reference authors "and figures from the same corpus") — years of monthly runway, not an immediate problem, but not something to assume is inexhaustible either.

### 4. The prompt mixes Markdown headings with two embedded tags — a genuinely new combination for this project, flagged as a real testing risk

Every prompt so far has been one or the other: fully Markdown headings (human-read only: `s4 briefing`, ADR 007's homeostat) or fully XML tags (code-parsed: `brief`). This prompt needs both at once — Markdown structure, because the raw output is meant to be read and eventually sent as-is; two embedded tags, because two specific values need exact code-side extraction. Nothing in this project's prompt-reliability history (ADR 002, ADR 003, ADR 006) has tested this exact combination.

```
## Noticias de la editorial
[Novedades del mes: títulos que avanzaron en producción, hitos de distribución, cualquier nota proporcionada para este mes.]

## Explorador del mes
<explorer>Nombre completo</explorer>
[Biografía breve — 2-3 párrafos — y por qué encaja con el catálogo de la editorial.]

## Plato del mes: cocina de Asia y Asia Central
<dish>Nombre del plato</dish>
[Breve descripción del plato, su origen, y su conexión con la región que cubre la editorial.]
```

Flagged explicitly rather than assumed to work first try — expect this may need the same kind of iteration ADR 003's `briefing` prompt needed (three rounds beyond the ADR 002 baseline) before the tags reliably survive alongside the headings. Budgeted for in the Implementation Checklist, not treated as a risk to design around speculatively before real testing shows what actually breaks.

### 5. `metadata_footer: contact_facts` reused as-is for a real contact block

Same mechanism `press-dossier` already uses (ADR 002 point 5) — the newsletter's closing "how to reach us" is appended in code from `templates/marketing_metadata.yaml`'s `contact_facts` block, never guessed by the model. Zero new code.

### 6. Amendment to `lib/orchestrator.py`: book-scoped orchestration must skip non-book tasks living in a book-scoped system's `tasks.yaml`

This is the one real wrinkle System 5 never had to face. `s5`'s tasks (ADR 006's `evaluate`, ADR 007's homeostat trio) were automatically excluded from System 2's book orchestrator because `s5` was never in `BOOK_SYSTEMS = ("s1b", "s1d")` to begin with. Newsletter's tasks live inside `s1d`'s `tasks.yaml` — a system that *is* in `BOOK_SYSTEMS` — so without a fix, `orchestrator._load_graph()` would silently pull `newsletter-scan`/`newsletter`/`newsletter-track` into *every* book's task graph, and worse, `s2 run <book_slug>` would try to execute them against that book's root instead of `newsletter_root()`.

Fixed with one new optional field, `book_scoped: false` (default `true` — every existing task is unaffected), and one filter in `_load_graph()`:

```python
def _load_graph() -> list:
    graph = []
    for system in BOOK_SYSTEMS:
        tasks = [t for t in task_loader.load_system_tasks(system) if t.get("book_scoped", True)]
        ...
```

`book_scoped` also needs adding to `task_loader.py`'s `_RESERVED_KEYS`, alongside `name`/`engine`/`output`/`input` — otherwise it would be passed to the engine as an unexpected keyword argument. This is a small, general fix: any future periodic task added to a book-scoped system's `tasks.yaml` gets the same one-line opt-out, not a newsletter-specific hack.

### 7. `s2` integration from the start — applying ADR 007's own lesson before it has to be relearned

ADR 007 was revised mid-review because leaving a three-stage periodic pipeline as three hand-sequenced commands was exactly the problem System 2 exists to solve. Same shape here, so it's built in from the start this time: `pipeline.py s2 newsletter status` / `s2 newsletter run [--step]`, following ADR 007's `HOMEOSTAT_TASKS`/`run_homeostat()`/`homeostat_status()` pattern exactly — an explicit ordered task list, unconditional re-execution (periodic, not one-and-done), stop immediately on failure (linear chain, no independent siblings to protect).

```python
# lib/orchestrator.py — additions
NEWSLETTER_TASKS = [("s1d", "newsletter-scan"), ("s1d", "newsletter"), ("s1d", "newsletter-track")]

# run_newsletter() / newsletter_status(): structurally identical to
# run_homeostat() / homeostat_status() (ADR 007) — see Alternatives for why
# this is deliberately not unified into one shared helper yet.
```

```
pipeline.py s2 newsletter status
pipeline.py s2 newsletter run [--step]
```

---

## Alternatives Considered

- **Not unifying `run_homeostat()`/`run_newsletter()` into one shared `run_periodic_chain()` helper now, even though the two are structurally identical** — genuinely tempting, since ADR 007 named "a second non-book periodic project shows up" as the exact trigger for building that generalization, and this is that second one. Deferred anyway: both ADRs are still unimplemented and unapproved: coupling their implementations together (whichever lands first would need to build the shared helper on behalf of the one that hasn't landed yet) adds review friction for a small amount of duplicated code. The unification is a good, low-risk follow-up once both exist for real — flagged here so it isn't forgotten, not done speculatively across two pending ADRs.
- **Fully automatic news gathering only, no human notes file** — rejected per the direction already given: loses anything the pipeline has no way to know (a press mention, a partnership), which is exactly the kind of fact this project's own principles say should never be guessed.
- **HTML/email-ready rendering, matching ADR 007's dashboard treatment** — rejected for v1 per the direction already given: matches every other System 1D output's existing shape; revisit if Markdown copy-paste turns out to be real friction in practice.
- **Fuzzy name-matching for the exclusion check** (to catch "Sven Hedin" vs. "Sven A. Hedin")** — rejected: a new dependency and real complexity for a low-stakes, human-reviewed mistake. Exact case-insensitive matching, accepted as a known limit (point 3).
- **Keeping the whole output XML-tagged (like `brief`), with a separate rendering step producing the human-readable Markdown** — considered, rejected: adds a fourth pipeline stage for a case none of this project's existing multi-section prompts have actually needed; the mixed-headings-plus-two-tags approach (point 4) is smaller if it holds up in testing, and is exactly the kind of thing to find out by testing rather than by pre-emptively engineering around an untested risk.
- **A `newsletter new` bootstrap command for the monthly notes file, mirroring `candidate new`** — rejected: `newsletter-scan` already creates the `notes/` folder and degrades gracefully when this month's file is missing (point 2); a dedicated bootstrap command would be one more thing to remember for a step that's genuinely optional.

---

## Consequences

**Easier:**
- A recurring task that currently costs the Editorial Director real manual time gets a first-pass draft automatically, grounded in real production data plus whatever news they choose to add.
- Down to two commands for the whole cycle (`s2 newsletter run`, then reading the final copy) — same reduction ADR 007 gives homeostat, applied here from the start rather than after a review round-trip.
- Zero new dependencies — reuses `llm_text.py`, `lib.dashboard.portfolio_summary()`, the `<fuente>`-tag convention, and `metadata_doc.py`'s regex-extraction precedent.

**Harder / needs care:**
- Point 4's mixed Markdown-plus-tags prompt shape is genuinely new for this project — budget real iteration time during implementation, the same way ADR 003's `briefing` prompt needed three rounds beyond its ADR 002 baseline before it held.
- Non-repetition is enforced by exact string matching, not identity — a renamed or slightly-differently-spelled repeat will slip through with only a human catching it, same as any advisory output in this project.
- `book_scoped: false` (point 6) is a new concept in the `tasks.yaml` schema that only matters for systems already inside `BOOK_SYSTEMS` — worth remembering when any future task is added to `s1b`/`s1d` that isn't a per-book transformation.
- The "books completed this month" signal reuses ADR 005's already-named honesty caveat: it reflects pipeline activity (tasks finishing), not confirmed real-world publication — the same gap already documented for the S3 dashboard's "in-pipeline span."
- Duplicated runner logic against ADR 007 (see Alternatives) is a small, deliberate, tracked piece of technical debt, not an oversight.

---

## Implementation Checklist

- [ ] Add `newsletter_dir: newsletter` to `config.example.yaml`/`config.yaml`; add `paths.newsletter_root(config)` to `lib/paths.py`
- [ ] Add `book_scoped` to `task_loader.py`'s `_RESERVED_KEYS`; filter on it (default `True`) in `orchestrator._load_graph()` (point 6)
- [ ] Create `engines/newsletter_scan.py` (`CLI_ARG = "none"`): filter `lib.dashboard.portfolio_summary()` by current-month activity, read this month's notes file (warn-and-continue if absent), read both tracking files; write tag-delimited `combined.txt`
- [ ] Create `prompts/s1d/newsletter_task.txt` (real, Spanish) and `prompts/examples/s1d/newsletter_task.txt` (English reference) per point 4
- [ ] Create `engines/newsletter_track.py`: extract `<explorer>`/`<dish>`, check + update `featured_explorers.yaml`/`featured_dishes.yaml` (warn, don't fail, on a repeat), write the tag-stripped final copy
- [ ] Add `newsletter-scan`, `newsletter`, `newsletter-track` to `systems/s1d/tasks.yaml` (all `book_scoped: false`)
- [ ] Add `NEWSLETTER_TASKS`, `run_newsletter()`, `newsletter_status()` to `lib/orchestrator.py`, mirroring ADR 007's homeostat functions
- [ ] Add a `newsletter` subgroup under `s2` in `pipeline.py`: `s2 newsletter status`, `s2 newsletter run [--step]`
- [ ] Add `/newsletter/` to `.gitignore`, alongside the other four perpetual roots
- [ ] End-to-end test: run against real (or realistic disposable) data for two consecutive months; confirm the second month's exclusion lists correctly steer the model away from the first month's explorer/dish; deliberately force a repeat (e.g. an exhausted or narrow test policy) and confirm `newsletter-track` warns rather than crashing; confirm the final copy has no visible tag markup; iterate the prompt per point 4 until headings and tags both hold reliably across at least two real runs
- [ ] Update README with the System 1D newsletter section, the `s2 newsletter` commands, and the command reference table
