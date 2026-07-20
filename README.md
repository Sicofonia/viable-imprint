# Viable Imprint

A command-line editorial pipeline for small literary imprints, built on Stafford Beer's Viable System Model.

---

## Inspiration

This project starts from a practical problem: running a micro-imprint that recovers 19th and early 20th century travel and exploration literature from Asia and Central Asia is editorial work, not administrative work — but the director's time disappears into OCR cleanup, translation coordination, formatting, and marketing copy before a single sentence of critical judgement has been exercised.

Stafford Beer's Viable System Model offers a way to think about this clearly. A viable organisation is one where the parts that create value (System 1) are not being strangled by the coordination overhead that is supposed to serve them. In a two-person imprint, System 1 *is* the bottleneck: every title passes through text preparation, copyediting, translation, layout handoff, and publication marketing — sequentially, manually, on the director's desk.

This pipeline automates the repeatable parts of System 1 so that editorial judgement can be spent on what automation cannot do: choosing the right edition of a text, writing the contextual introduction, deciding whether a 1907 account of the Tarim Basin deserves a general or specialist readership.

The pipeline is intentionally **dumb and sequential**. Each command does one thing, writes its output to a folder, and stops. You inspect the result before running the next command. There is no orchestration layer, no state machine, no background process. The filesystem is the record.

---

## Open Source

This project is LLM-agnostic and translation-provider-agnostic by design. The provider abstraction (`providers/llm/` and `providers/translation/`) means that swapping Mistral for Anthropic, or DeepL for LibreTranslate, requires adding one file and one line in `config.yaml` — no changes to the pipeline steps themselves.

The same principle applies to document generation: the formatting step uses `odfpy` (pure Python, no system dependencies) but the interface is narrow enough to replace with a LibreOffice/uno implementation if you need template-based styling.

Contributions are welcome. If you add a provider, please follow the existing pattern: implement the base class, register it in `providers/__init__.py`, and document the required config keys in `config.example.yaml`.

---

## Architecture

The pipeline is organised in four layers:

**`providers/`** — thin wrappers around external APIs. Each provider implements a two-method interface (`complete` for LLMs, `translate` for translation engines). The rest of the code only ever calls those methods.

**`lib/`** — shared utilities: paragraph-boundary chunking for long texts, per-book manifest tracking (including the System 2 run-state ledger and its System 3 metrics fields — see below), ODT generation, path resolution, loading/rendering the bibliographic-fact blocks used by `s1d`'s marketing tasks, the System 2 task-graph orchestrator (`lib/orchestrator.py`), the System 3 cost/duration capture (`lib/metrics.py`), the System 3 dashboard aggregation (`lib/dashboard.py`), and shared data access for the System 5 homeostat pipeline (`lib/homeostat.py` — finding System 4's latest briefing, reading/writing the decision log).

**`engines/`** — the generic execution logic, shared across every system: `llm_text` (chunk input, call an LLM with a given prompt file — reused by thirteen different tasks across three systems, each just pointing it at a different prompt), `translation` (chunk input, call a translation provider), `odt_format` (render markup to `.odt`), `metadata_doc` (assemble a document from bibliographic facts with no LLM call at all), `feed_scan` (pull new items from a curated watchlist of external sources — RSS/Atom feeds, Project Gutenberg's bulk catalog, and an imprint's own catalog page — no LLM call), `homeostat_scan` (gather System 3/4/decision-log data into one file, no LLM call), `homeostat_render` (render the final self-contained dashboard HTML, no LLM call), `newsletter_scan` (gather this month's production activity, notes, and non-repetition tracking lists into one file, no LLM call), `newsletter_track` (check/record the featured explorer and dish, write the final clean copy, no LLM call). Engines are written once and reused by any task that needs that shape of work.

**`systems/`** — one subfolder per VSM system, each holding a single `tasks.yaml` manifest. Only systems with implemented tasks exist here; a system is added when its first task is written. Each manifest entry names a task, the engine it uses, and that engine's parameters (typically a prompt file):

- `systems/s1b/tasks.yaml` — System 1B (Editorial Production): cleanup, translate, ortho, copyedit, format
- `systems/s1d/tasks.yaml` — System 1D (Publication and Marketing): brief, synopsis, story-map, one-pager, press-dossier, trailer-storyboard, goodreads-profile, metadata (see `docs/adr/002-marketing-brief-pipeline.md` for why marketing is a chain of small tasks rather than one big one). Also `newsletter-scan`, `newsletter`, `newsletter-track` (see `docs/adr/008-system1d-newsletter.md`) — a periodic monthly newsletter, driven by `s2 newsletter run` rather than run by hand; see below
- `systems/s4/tasks.yaml` — System 4 (Strategic Intelligence): scan, briefing (see `docs/adr/003-system-4-strategic-intelligence.md`) — unlike S1B/S1D, System 4 isn't about any one book, so it doesn't live under `books/` at all; see below
- `systems/s5/tasks.yaml` — System 5 (Identity, Values and Policy): evaluate (see `docs/adr/006-system-5-policy-agent.md`) — reads a candidate text's description and returns an advisory fit/borderline/reject verdict against the editorial policy in `docs/vsm.md`. Not book-scoped either; see below. Also `homeostat-scan`, `homeostat`, `homeostat-render` (see `docs/adr/007-system5-homeostat-dashboard.md`) — a periodic S3/S4 confrontation dashboard, driven by `s2 homeostat run` rather than run by hand; see below

Adding a new LLM-driven editorial task to a system requires writing a prompt file and adding a few lines to that system's `tasks.yaml` — no Python. The CLI itself is built dynamically from these manifests at startup (see `lib/task_loader.py`), so `pipeline.py <system> --help` always reflects whatever that system is currently configured to do.

**`pipeline.py s2`** is the one command group that isn't manifest-driven — System 2 (Coordination) doesn't declare its own tasks, it coordinates the ones System 1B and System 1D already declare. It reads every book-scoped `tasks.yaml` to build a cross-system task graph (each task's `input:` field, or positional chaining within a system when `input:` is absent) and checks it against a run-state ledger recorded in the book's own `manifest.yaml`. `pipeline.py s2 status <book>` shows what's done, stale, ready, or blocked (and why); `pipeline.py s2 run <book>` runs every currently-unblocked or stale task, across both systems, until nothing further is unblocked — turning thirteen hand-typed commands into one. See `docs/adr/004-system-2-run-orchestration.md`.

**The ledger tracks validity, not just completion (ADR 009).** Re-running a task automatically flags everything downstream of it, transitively and across systems, as `stale` if it was previously `done` — re-running `s1b cleanup` after a prompt fix correctly stales `translate` → `format`, and re-running `s1d brief` stales all seven of its fan-out siblings (`synopsis`, `story-map`, ...) in one go, even though none of them chain to each other. A `stale` task is not "done" as far as `s2 run` is concerned — the next `s2 run` re-walks and clears the whole stale chain the same way it would a fresh book. Separately, `s2 status` flags a `done`/`stale` output file as `(edited since run)` if its mtime no longer matches what was recorded at completion — informational only, since hand-editing an output is normal use of this pipeline, not something to block on. See `docs/adr/009-system-2-invalidation.md`.

**`pipeline.py s3`** is System 3 (Performance Monitoring) — also hand-written, also read-only. Every task run (manual or via `s2 run`) now records its duration, and — for the two engines that call an external provider (`llm_text`, `translation`) — token/character usage, provider, model, and an optional cost in USD, right alongside its `s2` ledger entry. `pipeline.py s3 dashboard` shows a portfolio-wide summary across every book; `pipeline.py s3 dashboard <book>` breaks one book down task by task. Cost is opt-in: it's only computed when you've filled in an optional `pricing:` block in `config.yaml` (illustrative placeholders ship in `config.example.yaml` — verify against your actual plan before trusting the numbers); duration and raw usage are captured either way. This is deliberately scoped to *reporting* only — no resource allocation, budget tracking, or pricing decisions, which stay a human call. See `docs/adr/005-system-3-performance-monitoring.md`.

**`pipeline.py s5 evaluate`** is System 5 (Identity, Values and Policy) — the one manifest-driven task whose prompt is committed rather than gitignored, since it's substantively the same editorial policy already public in `docs/vsm.md`. It reuses `engines/llm_text.py` completely unchanged: the "policy agent" is just `vsm.md`'s System 5 section reformatted as a system prompt. Given a candidate's description (title, author, approximate date, subject, what's known about rights/source quality), it returns an advisory verdict — fits / borderline / doesn't fit — reasoned against the thematic scope and non-negotiable values in `docs/vsm.md`. It does **not** decide anything: no candidates database, no automatic book creation, nothing gated. **`pipeline.py candidate new <slug>`** is the bootstrap (mirroring `init`, but for a candidate rather than a book) — it does no System 1A work itself (identifying or sourcing a candidate is still entirely a human task today); it just creates the folder a brief goes in. See `docs/adr/006-system-5-policy-agent.md`, especially point 8, on why that distinction is worth keeping clear.

**`pipeline.py s2 homeostat`** drives System 5's other advisory artifact — the homeostat dashboard, Beer's System 3/4 conflict-arbitration role given a concrete shape (`docs/adr/007-system5-homeostat-dashboard.md`). Three tasks (`homeostat-scan` → `homeostat` → `homeostat-render`) gather System 3's portfolio snapshot, System 4's latest briefing, and a decision log into a single self-contained `homeostat.html` — no server, no scheduler, a plain HTML file you open in a browser. Unlike book production, this pipeline is periodic, not one-and-done: `s2 homeostat run` re-executes the whole chain every time, producing a fresh dated snapshot, rather than skipping steps already marked `done` the way `s2 run <book_slug>` does. `pipeline.py s5 log-decision "<tension>" "<decision>"` appends one line to the decision log the dashboard displays — the one genuinely human step, kept as a plain two-argument command rather than a form.

**`pipeline.py s2 newsletter`** drives System 1D's monthly newsletter (`docs/adr/008-system1d-newsletter.md`) — the same periodic shape as the homeostat pipeline, one system over: `newsletter-scan` (gather this month's production activity, the director's optional monthly notes, and two non-repetition tracking lists) → `newsletter` (`llm_text`, drafts an explorer-of-the-month spotlight, a dish-of-the-month cultural feature, and imprint news, in that order) → `newsletter-track` (checks the featured explorer/dish against the tracking lists — warns rather than fails on a repeat — records new ones, and writes the final copy with its tracking tags stripped, ready to paste into whatever you actually send through). These three tasks live in `systems/s1d/tasks.yaml` alongside System 1D's book-scoped tasks but are marked `book_scoped: false`, so `s2 run <book_slug>` never pulls them into a book's task graph, and `s2 newsletter run` is the only thing that drives them in sequence — matching System 1D's own ownership at the CLI level (`pipeline.py s1d newsletter-scan` etc. still work directly) while System 2 handles the sequencing, the same relationship it already has with every other system's tasks.

**Every task's output lands under the VSM system that produced it** (`s1b/`, `s1d/`), mirroring the CLI's own nested command structure (`pipeline.py s1b <task>`, `pipeline.py s1d <task>`) — a book's folder never mixes editorial-production output with marketing output at the same level. `manifest.yaml` is the one exception, staying at the book root since it's shared across every system. Cross-system chaining still works exactly as you'd expect: `s1d brief`, for instance, reads from `s1b/copyedit/es/`, and its own output lands under `s1d/`, not `s1b/`.

| Task | VSM | Input | Output |
|------|-----|-------|--------|
| `s1b cleanup` | System 1B | Raw OCR `.txt` in `s1b/source/` | Cleaned `.txt` with `[i]`/`[sc]` markup in `s1b/cleaned/` |
| `s1b translate` | System 1B | Cleaned `.txt` | Translated `.txt` in `s1b/translated/es/` |
| `s1b ortho` | System 1B | Translated `.txt` | Orthotypographic corrections in `s1b/ortho/es/` |
| `s1b copyedit` | System 1B | Ortho-corrected `.txt` | Copy-edited `.txt` in `s1b/copyedit/es/` |
| `s1b format` | 1B → 1C handoff | Any corrected `.txt` | Formatted `.odt` in `s1b/formatted/` |
| `s1d brief` | System 1D | Copy-edited `.txt` (from `s1b/copyedit/es/`) | Extraction brief in `s1d/brief/es/` — reads the full manuscript once; every task below reads this brief, not the manuscript |
| `s1d synopsis` | System 1D | Brief | Back-cover synopsis in `s1d/synopsis/es/` |
| `s1d story-map` | System 1D | Brief | Six-section reference doc (intro, biography, waypoints, chronology, temples, ethnic groups) in `s1d/story-map/es/`, for hand-building a map in ArcGIS or similar |
| `s1d one-pager` | System 1D | Brief | One-page info sheet (highlights, summary, author bio) in `s1d/one-pager/es/` |
| `s1d press-dossier` | System 1D | Brief + `marketing_metadata.yaml` | Press dossier in `s1d/press-dossier/es/` — bibliographic facts and contact details are merged in from config, never written by the LLM |
| `s1d trailer-storyboard` | System 1D | Brief | Two scene-by-scene storyboards (60s and 90s) for a YouTube book trailer, in `s1d/trailer-storyboard/es/` |
| `s1d goodreads-profile` | System 1D | Brief | Description, author bio, and suggested shelves in `s1d/goodreads-profile/es/`, meant to be copy-pasted into Goodreads by hand (there is no API for this — see Setup) |
| `s1d metadata` | System 1D | Brief + `marketing_metadata.yaml` | Bibliographic reference sheet in `s1d/metadata/es/` — pure data assembly, no LLM call |
| `s4 scan` | System 4 | Nothing (external sources per `systems/s4/sources.yaml`) | New items since the last scan in `intelligence/s4/scan/<date>/` — no LLM call, no book involved at all |
| `s4 briefing` | System 4 | That day's scan | Strategic intelligence briefing in `intelligence/s4/briefing/<date>/` |
| `s5 evaluate` | System 5 | A candidate's description in `candidates/s1a/briefs/` (create with `candidate new <slug>`) | Advisory fit/borderline/reject verdict in `candidates/s5/evaluate/` — no book involved, no automatic decision |
| `s5 homeostat-scan` | System 5 | Nothing (reads S3/S4/decisions directly) | Combined snapshot in `homeostat/s5/homeostat-scan/<date>/` — no LLM call |
| `s5 homeostat` | System 5 | That scan | Tensions/tradeoffs narrative in `homeostat/s5/homeostat/<date>/` |
| `s5 homeostat-render` | System 5 | That narrative + fresh S3/S4/decision data | Self-contained `homeostat.html` in `homeostat/s5/homeostat-render/<date>/` — no LLM call |
| `s1d newsletter-scan` | System 1D | Nothing (reads S3 activity, notes, tracking lists directly) | Combined snapshot in `newsletter/s1d/newsletter-scan/<date>/` — no LLM call |
| `s1d newsletter` | System 1D | That scan | Draft (explorer, dish, news) in `newsletter/s1d/newsletter/<date>/` |
| `s1d newsletter-track` | System 1D | That draft | Final, tag-stripped copy in `newsletter/s1d/newsletter-track/<date>/` — no LLM call, updates non-repetition tracking |

**`prompts/`** — plain text task prompts for the LLM calls, one folder per VSM system (`prompts/s1b/`, `prompts/s1d/`, `prompts/s4/`) so prompts don't get lumped together as the project grows. Edit these to tune editorial behaviour without touching Python. The live prompts are gitignored, since they encode a specific imprint's editorial voice; only `prompts/examples/<system>/` is committed, as reference material. **`prompts/s5/policy_evaluation_task.txt` is the one exception — it's committed**, since it's substantively the same editorial policy already public in `docs/vsm.md`, not personal-but-unpublished editorial voice; see `docs/adr/006-system-5-policy-agent.md`, point 7. See Setup below.

**`templates/`** holds two independent things, both gitignored (only their `.example` counterparts are committed):

- `format_styles.yaml` — `format` doesn't build a document from scratch; it loads your own `.odt` template (page setup, margins, and named paragraph styles already defined) and appends the manuscript to it, using your template's own style names. This file maps a small set of structural roles — `chapter_number`, `chapter_title`, `first_paragraph`, `body` — to whatever you've actually named those styles in your template. Chapter structure is detected directly from the text (a short, all-uppercase line containing a roman numeral or digit is the chapter number; an immediately following short uppercase line is the title) — no markup tags required. Inline `[i]`/`[sc]`/`[FN: ...]` markup is left as plain visible text in the output by design; applying that formatting is a manual step.
- `marketing_metadata.yaml` — bibliographic and contact facts (author, ISBN, price, launch date, email, links...) for a single book, grouped into named blocks. `s1d press-dossier` and `s1d metadata` read this; neither ever asks an LLM to guess a fact that belongs here. Add, rename, or drop blocks freely — see `lib/metadata_blocks.py` and `docs/adr/002-marketing-brief-pipeline.md` for how a prompt requests a block by name.

Each book lives in its own folder under `books/`. A `manifest.yaml` in each folder records which files have been processed, by which provider and model, so the record travels with the text. It also carries a `tasks:` block — the System 2 run-state ledger, keyed as `<system>.<task-name>` (e.g. `s1b.cleanup`, `s1d.brief`), recording each task's status (`done`/`stale`/`failed`), output path, and timestamp. This is written automatically by every task run, whether invoked manually (`s1b cleanup <file>`) or through `s2 run` — it's what `s2 status`/`s2 run` read to know what's already done. Since ADR 005, each entry also carries `duration_seconds`, and — for tasks whose engine calls an external provider — `provider`, `model`, `usage` (tokens or characters), and `cost_usd` (`null` if no pricing is configured); this is what `s3 dashboard` reads. Since ADR 009, completing a task also cascade-flags anything downstream still marked `done` as `stale` (not to be trusted until it's rerun) and records the output file's mtime, so `s2 status` can flag a `done`/`stale` file that's been hand-edited since — see below.

```
books/
└── life-as-explorer/
    ├── manifest.yaml          # the one file shared across every system
    ├── s1b/
    │   ├── source/
    │   ├── cleaned/
    │   ├── translated/es/
    │   ├── ortho/es/
    │   ├── copyedit/es/
    │   └── formatted/
    └── s1d/
        ├── brief/es/
        ├── synopsis/es/
        ├── story-map/es/
        ├── one-pager/es/
        ├── press-dossier/es/
        ├── trailer-storyboard/es/
        ├── goodreads-profile/es/
        └── metadata/es/
```

**System 4 doesn't operate on a book at all**, so it doesn't live under `books/`. `systems/s4/sources.yaml` (gitignored; `sources.example.yaml` committed) declares a curated watchlist — RSS/Atom feeds, Project Gutenberg's own bulk catalog dump (filtered by subject keywords, not scraped), and your own catalog page as a standing reference for spotting genuine gaps. `scan` and `briefing` write into a single perpetual `intelligence/` folder at the repo root, gitignored like `books/`, structured the same way a book is internally (its own `manifest.yaml`, a `s4/` subfolder) even though it isn't one:

```
intelligence/
├── manifest.yaml
└── s4/
    ├── scan/
    │   ├── _state.yaml       # last-seen item per source, persists across runs
    │   └── 2026-07-04/
    │       ├── valordecambio.txt
    │       ├── gutenberg.txt
    │       ├── <your-catalog>.txt
    │       └── combined.txt   # what `briefing` reads
    └── briefing/
        └── 2026-07-04/
            └── combined.txt
```

See `docs/adr/003-system-4-strategic-intelligence.md` for why System 4 needed this rather than fitting the book-scoped shape S1B/S1D use, and for the reliability lessons that shaped `scan`'s output format (worth reading before writing a new prompt whose input is `scan`'s output).

**A text under System 5 evaluation isn't a book either** — there's no editorial commitment to acquire it yet, so it doesn't belong under `books/`. `pipeline.py candidate new <slug>` creates a perpetual `candidates/` folder at the repo root (gitignored like `books/`/`intelligence/`), structured like a book internally for the same reason System 4's `intelligence/` is:

```
candidates/
├── manifest.yaml
├── s1a/
│   └── briefs/
│       └── <candidate-slug>.txt      # you write this by hand
└── s5/
    └── evaluate/
        └── <candidate-slug>.txt      # the verdict
```

`s1a/briefs/` is a landing spot for the brief, not evidence that System 1A is implemented — identifying and sourcing a candidate is still entirely manual today. See `docs/adr/006-system-5-policy-agent.md`, point 8, for why that line is worth keeping clear.

**The homeostat dashboard gets a fifth perpetual root**, `homeostat/`, same shape as the other four — a periodic artifact confronting System 3 and System 4 has no book, and no single date, to belong to:

```
homeostat/
├── manifest.yaml
├── decisions.yaml                        # flat, append-only — `s5 log-decision` appends here
└── s5/
    ├── homeostat-scan/2026-08-01/combined.txt
    ├── homeostat/2026-08-01/combined.txt        # tensions/tradeoffs narrative
    └── homeostat-render/2026-08-01/combined.html # the actual dashboard
```

Run the whole thing with `pipeline.py s2 homeostat run`, then open the resulting `.html` file directly in any browser — it's fully self-contained (inline CSS, inline SVG chart, no external requests), so it works offline and travels as a single file. See `docs/adr/007-system5-homeostat-dashboard.md` for the full design, including why this pipeline is *not* orchestrated the same way book production is (it's meant to be redone every period, not skipped once "done").

**The monthly newsletter gets a sixth perpetual root**, `newsletter/`, same periodic reasoning as `homeostat/` — it's calendar-driven and catalog-wide, not tied to any one book:

```
newsletter/
├── manifest.yaml
├── featured_explorers.yaml                 # flat, append-only — non-repetition tracking
├── featured_dishes.yaml                    # flat, append-only — non-repetition tracking
├── notes/
│   └── 2026-08.txt                         # optional, human-written monthly notes
└── s1d/
    ├── newsletter-scan/2026-08-01/combined.txt
    ├── newsletter/2026-08-01/combined.txt        # draft — tags still in place
    └── newsletter-track/2026-08-01/combined.txt  # final, clean copy — the actual deliverable
```

Run it with `pipeline.py s2 newsletter run`; the final copy under `newsletter-track/<date>/` is ready to paste into whatever you actually send the newsletter through (Mailchimp, a plain email, your own site) — Markdown only for v1, matching every other System 1D output. See `docs/adr/008-system1d-newsletter.md` for the full design, including how repeating a previously-featured explorer or dish is prevented (an exclusion list in the prompt) *and* verified (a code-side check on the model's actual choice, since a model will occasionally ignore an instruction like this).

---

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) — manages Python version and dependencies automatically
- A Mistral API key (the pipeline calls the Mistral API directly over HTTP — no SDK required)
- A DeepL API key

**Python version:** the project is pinned to Python 3.12 via `.python-version`. Do not run it with Python 3.13 or 3.14 — some dependencies do not install correctly on those versions yet. `uv` handles this for you automatically.

---

## Setup

**1. Install `uv`**

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close and reopen your terminal after installing so the PATH updates.

**2. Clone and install**

```bash
git clone https://github.com/your-org/viable-imprint.git
cd viable-imprint
uv sync
```

`uv sync` fetches Python 3.12 if needed, creates a `.venv`, and installs all dependencies in one step.

**3. Configure**

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

Open `.env` and add your API keys:

```
MISTRAL_API_KEY=your-mistral-key-here
DEEPL_API_KEY=your-deepl-key-here
```

Keys must be unquoted and have no trailing spaces. DeepL free-plan keys end in `:fx` — paste them as-is and the client routes to the correct endpoint automatically.

Optionally, fill in the `pricing:` blocks under `llm:`/`translation:` in `config.yaml` with your actual per-provider rates to enable `s3 dashboard`'s cost column (System 3, ADR 005) — leave them out and it still works, just without a dollar figure.

**4. Add your task prompts**

Task prompts are personal editorial content — they encode your house style, language, and rules — so they are gitignored and not committed to this repository. Only the reference prompts in `prompts/examples/` ship with the project. Create your own by copying the examples and editing them:

```bash
cp prompts/examples/s1b/cleanup_task.txt prompts/s1b/cleanup_task.txt
cp prompts/examples/s1b/ortho_task.txt prompts/s1b/ortho_task.txt
cp prompts/examples/s1b/copyedit_task.txt prompts/s1b/copyedit_task.txt
cp prompts/examples/s1d/marketing_extract_task.txt prompts/s1d/marketing_extract_task.txt
cp prompts/examples/s1d/synopsis_task.txt prompts/s1d/synopsis_task.txt
cp prompts/examples/s1d/story_map_task.txt prompts/s1d/story_map_task.txt
cp prompts/examples/s1d/one_pager_task.txt prompts/s1d/one_pager_task.txt
cp prompts/examples/s1d/press_dossier_task.txt prompts/s1d/press_dossier_task.txt
cp prompts/examples/s1d/trailer_storyboard_task.txt prompts/s1d/trailer_storyboard_task.txt
cp prompts/examples/s1d/goodreads_profile_task.txt prompts/s1d/goodreads_profile_task.txt
```

Then open each file and rewrite it for your imprint's language and editorial rules. The example prompts are in English; nothing requires your own prompts to be — write them in whatever language you'll be editing in. `s1d metadata` has no prompt of its own — it's pure data assembly, see step 6.

Only copy the `s1d` prompts for deliverables you actually want; a task with no live prompt file simply isn't usable until you add one, and there's no requirement to use all eight. Deleting a task's entry from `systems/s1d/tasks.yaml` removes it from the CLI entirely.

**5. Set up your book template**

```bash
cp templates/format_styles.example.yaml templates/format_styles.yaml
```

Place your own `.odt` template at the path you set under `template:` in that file (default: `templates/book_template.odt`), then update the `styles:` mapping to match the actual paragraph style names defined in your template.

**6. Set up book metadata for marketing**

```bash
cp templates/marketing_metadata.example.yaml templates/marketing_metadata.yaml
```

Fill in this specific book's real bibliographic and contact facts (author, ISBN, price, launch date, email, links...). `s1d press-dossier` and `s1d metadata` read this file directly and will fail with a clear error if it's missing — this is by design, so a real fact is never silently replaced with an LLM's guess.

**7. Set up System 4's watchlist and briefing prompt**

```bash
cp systems/s4/sources.example.yaml systems/s4/sources.yaml
cp prompts/examples/s4/briefing_task.txt prompts/s4/briefing_task.txt
```

The example ships with two real, working feeds (`valordecambio.com`, Internet Archive's blog) and a working Project Gutenberg catalog filter — genuinely useful defaults for any public-domain-focused imprint, not placeholders. Point `catalog_reference` at wherever your own site lists your catalog, and tune the Gutenberg `subject_keywords` to your own niche (keep them geographically/topically specific — see the comments in the file for why).

---

## Running the pipeline

All commands are run via `uv run` — this ensures the correct Python and virtual environment are always used, regardless of what is active in your shell.

Tasks are grouped by VSM system: `pipeline.py s1b <task>` for editorial production, `pipeline.py s1d <task>` for publication and marketing. Run `uv run python pipeline.py s1b --help` at any time to see the tasks currently configured for that system.

**Initialise a book project:**

```bash
uv run python pipeline.py init life-as-explorer
```

Place your source `.txt` file in `books/life-as-explorer/s1b/source/`. Every other folder is created automatically the first time a task writes to it, nested under whichever system produced it.

**Let System 2 run the whole pipeline for you, or walk through it by hand:**

```bash
uv run python pipeline.py s2 status life-as-explorer   # what's done, what's next, what's blocked
uv run python pipeline.py s2 run life-as-explorer       # run every currently-ready task, across s1b and s1d
```

`s2 run` resolves each task's input from whatever the previous task produced (no retyping file paths), runs everything currently unblocked, and stops once nothing further is unblocked — independent tasks (e.g. System 1D's seven deliverables, which all read `brief`, not each other) keep going even if one of them fails; a failed task is simply retried the next time you run `s2 run`. Add `--only s1b` to restrict to one system, or `--step` to run exactly one ready task and stop — useful for reviewing `s1d brief` by hand before letting the (paid) expansion calls fire. See `docs/adr/004-system-2-run-orchestration.md`.

If you go back and re-run an earlier task by hand — say, fixing `prompts/s1b/cleanup_task.txt` and re-running `s1b cleanup` on a book already past `copyedit` — `s2 status` shows exactly what that invalidated:

```
System 1B — Editorial Production
  [x] cleanup              s1b/cleaned/life-as-explorer.txt
  [~] translate            stale (ready to rerun) — invalidated by s1b.cleanup
  [~] ortho                stale (blocked) — invalidated by s1b.cleanup
  [~] copyedit              stale (blocked) — invalidated by s1b.cleanup

System 1D — Publication and Marketing
  [~] brief                stale (blocked) — invalidated by s1b.cleanup
  [~] synopsis              stale (blocked) — invalidated by s1b.cleanup
  ...
```

`s2 run` treats `stale` the same as never-run, so a plain `s2 run life-as-explorer` re-walks and clears the whole stale chain in one call, cross-system fan-out included — no separate "invalidate" step to remember. See `docs/adr/009-system-2-invalidation.md`.

**Check what it cost:**

```bash
uv run python pipeline.py s3 dashboard                    # every book, compute time + API cost + in-pipeline span
uv run python pipeline.py s3 dashboard life-as-explorer   # one book, broken down task by task
```

Every task run — manual or via `s2 run` — records its duration and, for tasks that call an LLM or translation provider, token/character usage automatically. The cost column only fills in once you've set a `pricing:` block under `llm:`/`translation:` in `config.yaml` (see Setup); until then, duration and usage still show, cost just reads `-`. This is read-only reporting, nothing here allocates resources or makes pricing decisions — see `docs/adr/005-system-3-performance-monitoring.md`.

The rest of this section walks through the same pipeline one command at a time, useful for understanding what each step does or for rerunning a single one after a manual edit.

**Clean OCR text:**

```bash
uv run python pipeline.py s1b cleanup books/life-as-explorer/s1b/source/my-life.txt
```

Open `books/life-as-explorer/s1b/cleaned/my-life.txt`, read the first few pages, and verify the cleanup before continuing.

**Translate (English → Spanish):**

```bash
uv run python pipeline.py s1b translate books/life-as-explorer/s1b/cleaned/my-life.txt
```

**Orthotypographic corrections:**

```bash
uv run python pipeline.py s1b ortho books/life-as-explorer/s1b/translated/es/my-life.txt
```

**Copy-editing:**

```bash
uv run python pipeline.py s1b copyedit books/life-as-explorer/s1b/ortho/es/my-life.txt
```

**Generate the formatted ODT:**

```bash
uv run python pipeline.py s1b format books/life-as-explorer/s1b/copyedit/es/my-life.txt
```

**Generate the marketing extraction brief:**

```bash
uv run python pipeline.py s1d brief books/life-as-explorer/s1b/copyedit/es/my-life.txt
```

This is the first and only System 1D step that reads the manuscript — note that its input comes from `s1b/`, but its own output still lands under `s1d/`, not `s1b/`, since output always follows the *producing* task's system, not the input's. It produces a compact `s1d/brief/es/my-life.txt` — every other `s1d` task below reads *this file*, not the manuscript, which is what keeps generating seven marketing deliverables cheap regardless of how long the book is (see `docs/adr/002-marketing-brief-pipeline.md` for the full reasoning).

**Generate marketing deliverables:**

Each of these reads the brief you just generated. Run whichever ones you actually want, in any order — none of them depend on each other:

```bash
uv run python pipeline.py s1d synopsis books/life-as-explorer/s1d/brief/es/my-life.txt
uv run python pipeline.py s1d story-map books/life-as-explorer/s1d/brief/es/my-life.txt
uv run python pipeline.py s1d one-pager books/life-as-explorer/s1d/brief/es/my-life.txt
uv run python pipeline.py s1d press-dossier books/life-as-explorer/s1d/brief/es/my-life.txt
uv run python pipeline.py s1d trailer-storyboard books/life-as-explorer/s1d/brief/es/my-life.txt
uv run python pipeline.py s1d goodreads-profile books/life-as-explorer/s1d/brief/es/my-life.txt
uv run python pipeline.py s1d metadata books/life-as-explorer/s1d/brief/es/my-life.txt
```

`press-dossier` and `metadata` also read `templates/marketing_metadata.yaml` (Setup, step 6) and will stop with a clear error if it doesn't exist yet — bibliographic facts are merged in from that file, never generated by the LLM.

Want a ninth deliverable — a blog post, an email newsletter blurb, anything else built from the same brief? Write a prompt and add four lines to `systems/s1d/tasks.yaml`; no Python required. See `docs/adr/002-marketing-brief-pipeline.md` for the underlying design and the reliability lessons learned building the seven above (in particular: prompts whose output is only read by another LLM can use ordinary Markdown headings, but a prompt whose output gets parsed by *code* — like `brief`'s — needs XML-style tags instead, which held up far more reliably in testing).

**Scan your industry watchlist and generate a strategic intelligence briefing (System 4):**

Unlike every command above, `scan` doesn't take a file argument at all — there's no book involved, just your configured sources:

```bash
uv run python pipeline.py s4 scan
```

This writes `intelligence/s4/scan/<today>/combined.txt`. Then:

```bash
uv run python pipeline.py s4 briefing intelligence/s4/scan/<today>/combined.txt
```

There's no scheduler built into this pipeline (by design — see the Inspiration section above: no background process, the filesystem is the record). Run these two commands by hand, or wire them into an OS-level scheduled task (cron, Windows Task Scheduler) if you want a weekly cadence to happen automatically:

```bash
uv run python pipeline.py s4 scan && uv run python pipeline.py s4 briefing intelligence/s4/scan/$(date +%Y-%m-%d)/combined.txt
```

`scan` is resilient to a single source failing (a feed timing out, a site returning an error) — it warns and continues with the rest rather than aborting the whole run. See `docs/adr/003-system-4-strategic-intelligence.md` for the full design, including why Project Gutenberg needed its bulk catalog file rather than its RSS feed, and several further prompt-reliability lessons beyond the ones ADR 002 already found.

**Get a first-pass opinion on whether a candidate text fits your catalogue (System 5):**

```bash
uv run python pipeline.py candidate new life-of-a-nomad
```

This tells you where to write the candidate's description — title, author, approximate date, language, a paragraph on subject/scope, and whatever you know about its copyright status and source quality. It's free-form prose, not a rigid form. Then:

```bash
uv run python pipeline.py s5 evaluate candidates/s1a/briefs/life-of-a-nomad.txt
```

This writes a verdict (fits / borderline / doesn't fit) to `candidates/s5/evaluate/life-of-a-nomad.txt`, reasoned against the editorial policy in `docs/vsm.md`. It's advisory only — nothing here creates a book, updates a database, or decides anything for you; if you agree with the verdict and want to proceed, run `pipeline.py init life-of-a-nomad` yourself, same manual step as always. See `docs/adr/006-system-5-policy-agent.md`, and note point 8 in particular: this doesn't automate finding or vetting candidates, only checking one you've already found against policy.

**Confront System 3 against System 4 and get an actual dashboard, not another document (System 5):**

```bash
uv run python pipeline.py s2 homeostat run
```

This runs all three homeostat stages in order and writes `homeostat/s5/homeostat-render/<today>/combined.html` — open it in any browser. Unlike book production, running this again (even the same day) redoes the whole chain and produces a fresh snapshot; it never skips a stage because it was already `done`. `pipeline.py s2 homeostat status` shows each stage's last recorded outcome without re-running anything. After reading the dashboard:

```bash
uv run python pipeline.py s5 log-decision "S4 briefing flagged rising audiobook demand; S3 shows three titles already at full capacity." "Deferred to next quarter; no new title started this month."
```

This appends one line to `homeostat/decisions.yaml` — the next dashboard render shows it in the decision history. See `docs/adr/007-system5-homeostat-dashboard.md` for the full design, including why this needed its own orchestration mechanism distinct from `s2 run <book_slug>`.

**Generate the monthly newsletter (System 1D):**

```bash
uv run python pipeline.py s2 newsletter run
```

This drives `newsletter-scan` → `newsletter` → `newsletter-track` in order and writes the finished issue to `newsletter/s1d/newsletter-track/<today>/combined.txt` — a Markdown draft with an explorer-of-the-month spotlight, a dish-of-the-month feature, and imprint news, in that order, ready to paste into whatever you send it through. Optionally write `newsletter/notes/<YYYY-MM>.txt` beforehand with anything the pipeline can't know on its own (a press mention, a distribution milestone) — if it's missing, `newsletter-scan` warns and continues without it rather than failing. The explorer and dish are checked against `newsletter/featured_explorers.yaml`/`featured_dishes.yaml` and never knowingly repeated; a repeat that slips past the prompt anyway is flagged as a warning, not silently sent. See `docs/adr/008-system1d-newsletter.md` for the full design, including why these three tasks live in `systems/s1d/tasks.yaml` (System 1D still owns them — `pipeline.py s1d newsletter-scan` etc. work directly) even though `s2 newsletter run` is what actually sequences them, the same relationship System 2 already has with every other system's tasks.

---

## Licence

MIT
