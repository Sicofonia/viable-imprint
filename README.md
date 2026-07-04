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

**`lib/`** — shared utilities: paragraph-boundary chunking for long texts, per-book manifest tracking, ODT generation, path resolution, and loading/rendering the bibliographic-fact blocks used by `s1d`'s marketing tasks.

**`engines/`** — the generic execution logic, shared across every system: `llm_text` (chunk input, call an LLM with a given prompt file — reused by ten different tasks across both systems, each just pointing it at a different prompt), `translation` (chunk input, call a translation provider), `odt_format` (render markup to `.odt`), `metadata_doc` (assemble a document from bibliographic facts with no LLM call at all). Engines are written once and reused by any task that needs that shape of work.

**`systems/`** — one subfolder per VSM system, each holding a single `tasks.yaml` manifest. Only systems with implemented tasks exist here; a system is added when its first task is written. Each manifest entry names a task, the engine it uses, and that engine's parameters (typically a prompt file):

- `systems/s1b/tasks.yaml` — System 1B (Editorial Production): cleanup, translate, ortho, copyedit, format
- `systems/s1d/tasks.yaml` — System 1D (Publication and Marketing): brief, synopsis, story-map, one-pager, press-dossier, trailer-storyboard, goodreads-profile, metadata (see `docs/adr/002-marketing-brief-pipeline.md` for why marketing is a chain of small tasks rather than one big one)

Adding a new LLM-driven editorial task to a system requires writing a prompt file and adding a few lines to that system's `tasks.yaml` — no Python. The CLI itself is built dynamically from these manifests at startup (see `lib/task_loader.py`), so `pipeline.py <system> --help` always reflects whatever that system is currently configured to do.

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

**`prompts/`** — plain text task prompts for the LLM calls, one folder per VSM system (`prompts/s1b/`, `prompts/s1d/`) so prompts don't get lumped together as the project grows. Edit these to tune editorial behaviour without touching Python. The live prompts are gitignored, since they encode a specific imprint's editorial voice; only `prompts/examples/<system>/` is committed, as reference material. See Setup below.

**`templates/`** holds two independent things, both gitignored (only their `.example` counterparts are committed):

- `format_styles.yaml` — `format` doesn't build a document from scratch; it loads your own `.odt` template (page setup, margins, and named paragraph styles already defined) and appends the manuscript to it, using your template's own style names. This file maps a small set of structural roles — `chapter_number`, `chapter_title`, `first_paragraph`, `body` — to whatever you've actually named those styles in your template. Chapter structure is detected directly from the text (a short, all-uppercase line containing a roman numeral or digit is the chapter number; an immediately following short uppercase line is the title) — no markup tags required. Inline `[i]`/`[sc]`/`[FN: ...]` markup is left as plain visible text in the output by design; applying that formatting is a manual step.
- `marketing_metadata.yaml` — bibliographic and contact facts (author, ISBN, price, launch date, email, links...) for a single book, grouped into named blocks. `s1d press-dossier` and `s1d metadata` read this; neither ever asks an LLM to guess a fact that belongs here. Add, rename, or drop blocks freely — see `lib/metadata_blocks.py` and `docs/adr/002-marketing-brief-pipeline.md` for how a prompt requests a block by name.

Each book lives in its own folder under `books/`. A `manifest.yaml` in each folder records which files have been processed, by which provider and model, so the record travels with the text.

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

---

## Running the pipeline

All commands are run via `uv run` — this ensures the correct Python and virtual environment are always used, regardless of what is active in your shell.

Tasks are grouped by VSM system: `pipeline.py s1b <task>` for editorial production, `pipeline.py s1d <task>` for publication and marketing. Run `uv run python pipeline.py s1b --help` at any time to see the tasks currently configured for that system.

**Initialise a book project:**

```bash
uv run python pipeline.py init life-as-explorer
```

Place your source `.txt` file in `books/life-as-explorer/s1b/source/`. Every other folder is created automatically the first time a task writes to it, nested under whichever system produced it.

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

---

## Licence

MIT
