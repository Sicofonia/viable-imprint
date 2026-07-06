# ADR 003 — System 4: Strategic Intelligence Scanning and Briefing

**Status:** Implemented. Built and tested end-to-end against real sources (2026-07-04) — see "Implementation notes" for what changed versus the plan and the further reliability lessons found along the way.

---

## Context

`docs/vsm.md` describes System 4 as the function most easily neglected in a micro-imprint: System 1 always has something urgent, so environmental scanning — the thing that "guarantees long-term survival" — loses out to whatever book is on fire this week. Its stated responsibilities:

- Editorial market surveillance: emerging non-fiction topics, what comparable publishers are doing, what's getting media attention
- Tracking cultural and academic trends
- Exploring new formats and channels (audiobooks, courses, special editions, co-editions)
- Ecosystem relationships (universities, agents, other publishers, fairs)
- Catalogue planning 1–3 years out
- Assessing external risks (distribution shifts, market concentration, digitalisation, generative AI)

The user's framing: they lack time for this research, and want something like a "crawling bot," starting with summarizing industry news from sources like `valordecambio.com`.

**What this ADR is not**: a general-purpose web crawler. "Crawling bot" is the user's shorthand for the problem, not a specification. A handful of curated, known-good sources monitored on a light schedule is a better engineering fit than an open-ended spider — cheaper, lower legal/ethical risk, and actually matches what System 4 in `vsm.md` asks for ("monthly review of cultural and specialist press," not "index the internet"). This ADR proposes a **curated watchlist**, not a crawler, and explains why below.

---

## Research done before writing this

`valordecambio.com` was checked directly rather than assumed:

- It's a real, active, Spanish-language publishing-trade blog ("Información del mundo editorial") — publishing models, market data, digitalisation, AI-in-publishing, cultural policy. Genuinely on-target for System 4, not a guess.
- It has a valid, standard WordPress RSS 2.0 feed at `https://valordecambio.com/feed/` — title, link, pubDate, categories, description per item. Posts ~3–5×/week. Sample items already in the feed: a court ruling against Anna's Archive (a direct hit on "assessment of external risks" — copyright/piracy law affecting the whole industry), Spotify's audiobook push (a "new formats and channels" signal), and industry-ecosystem analysis pieces.
- The Internet Archive's own blog also has a feed at `blog.archive.org/feed/` — relevant to 1A's "periodic review of public-domain digital sources," which is System-4-adjacent (spotting *that* something changed, vs. 1A's job of deciding what to do about it).
- Publishers Weekly has RSS-enabled sections but I did not pin down and verify an exact feed URL — flagged as a starter-list candidate needing confirmation, not assumed working.

This matters for the design: RSS-first is not a theoretical preference, it's already available for the two sources most directly relevant to this imprint.

---

## Decision

### 1. Scope: automate the reading, not the judgment

Splitting `vsm.md`'s responsibilities by whether they're actually automatable:

| Responsibility | Automatable? |
|---|---|
| Market surveillance, trend tracking, risk assessment | **Yes** — this is "read a lot, extract what matters," exactly what an LLM pipeline already does well elsewhere in this project |
| New formats/channels, catalogue planning, ecosystem relationships | **Partially** — a good briefing surfaces raw signal ("Spotify is pushing audiobooks hard this quarter"), but the actual decision (should *we* do an audiobook?) stays human |
| Fair attendance, relationship-building | **No** — these are physically-embodied, human activities; automation isn't the point here |

The MVP scope is therefore: **a periodic scan of curated sources, synthesized into a briefing** — the same shape as every other automated piece of this project (System 1B automates transcription/translation/formatting, not editorial judgment; System 1D automates drafting marketing copy, not marketing strategy). System 4 automation should draft the *research*, not the *strategy document* — mirroring the "catalogue strategy document revised annually" practice `vsm.md` already names, which stays a human-authored artifact this briefing feeds into.

### 2. Two tasks, mirroring the System 1D pattern that already proved itself

- **`scan`** — no LLM call. Fetches each configured source's RSS/Atom feed, diffs against a persisted "last seen" record per source, and writes only the *new* items (title, link, date, description) to a dated, combined raw file. Mechanically identical in spirit to `translation` or `odt_format`: fetch/transform, no model involved.
- **`briefing`** — one LLM call over that combined raw file, synthesizing a structured intelligence briefing. This is a straight reuse of **`engines/llm_text.py`, completely unchanged** — same engine that already powers ten tasks across two systems. Just a new prompt file and a `tasks.yaml` entry. No new code needed for this half of the pipeline at all.

This is the same "extract once, then a cheap synthesis pass" shape ADR 002 established for `brief` → `synopsis`/etc., applied to a different kind of input (external sources instead of a manuscript).

### 3. System 4 isn't book-scoped — it needs its own perpetual project folder, not a new architecture

S1B and S1D both operate on one book at a time; their natural unit of work is a manuscript, and `lib/paths.book_root()` finds the right book by walking up from whatever file you hand it. System 4 has no manuscript — its unit of work is a time period, and its output belongs to the whole imprint, not one title.

Rather than inventing a new mechanism, System 4 gets a single **perpetual project folder at the repo root** — `intelligence/` — structured exactly like a book folder: its own `manifest.yaml`, its own `s4/` subfolder nesting (mirroring the `s1b/`/`s1d/` convention inside a book), dated run folders underneath:

```
intelligence/
├── manifest.yaml
└── s4/
    ├── scan/
    │   ├── _state.yaml          # last-seen item per source, persists across runs
    │   └── 2026-07-04/
    │       ├── valordecambio.txt
    │       ├── internet-archive-blog.txt
    │       ├── gutenberg.txt          # new catalog rows matching the subject keywords
    │       ├── ecosdeoriente-catalog.txt  # fresh snapshot, no diffing — reference, not "new items"
    │       └── combined.txt      # what `briefing` actually reads
    └── briefing/
        └── 2026-07-04/
            └── combined.txt
```

Because it's shaped like a book, **`briefing` needs zero changes to `llm_text.py`, `lib/paths.py`, or `lib/manifest.py`** — it just points at `intelligence/s4/scan/2026-07-04/combined.txt` the same way `s1d synopsis` points at a brief file, and `paths.book_root()` finds `intelligence/manifest.yaml` exactly as it would find a book's. Confirmed in testing down to a detail neither of us planned: `stage_output_dir`'s existing language-subfolder mirroring (built for `translated/es/`-style paths) treats the date segment in `scan/2026-07-04/combined.txt` exactly like a language code, so `briefing`'s output landed at `briefing/2026-07-04/combined.txt` — a per-date folder, for free, with no S4-specific logic at all.

`scan` is the one genuinely new shape: it doesn't take an input file at all (there's nothing to transform — it's pulling from external URLs per a config file). `lib/task_loader.py` currently hardcodes an `input_file` positional argument for every task; `scan` needs a task that takes none. This means reintroducing a non-default `CLI_ARG` mode in the task loader (a different need than the `"directory"` mode removed during the System 1D migration, though structurally similar) — an engine declaring `CLI_ARG = "none"` gets no positional argument, and `root` is resolved from config (`intelligence_dir`, defaulting to `intelligence`) rather than by walking up from a file. This is the one real piece of task-loader surgery this ADR asks for; everything else reuses existing mechanisms.

### 4. RSS-first, curated, no open-ended crawling

- **In scope for v1**: sources with a working RSS/Atom feed. Fetching a site's own published feed is exactly what it's for — no robots.txt question, no scraping ethics question, standard practice.
- **Out of scope for v1**: sources without a feed. A polling scraper for feed-less sites is a real future option, but it's a case-by-case legal/robots.txt/rate-limit decision per source, not a default fallback silently baked into the engine. Both confirmed starter sources (`valordecambio.com`, Internet Archive's blog) already have working feeds, so v1 doesn't need to cross this line at all.
- **No link-following.** `scan` reads each configured feed and nothing else — it does not follow outbound links, does not discover new sources on its own, does not paginate beyond what the feed itself returns. This is a watchlist, not a spider.

### 5. `sources.yaml` is personal config, like `format_styles.yaml` and `marketing_metadata.yaml`

A different imprint running this project would want a different watchlist (different language, different niche, no reason to know about `ecosdeoriente.com`). Gitignored `systems/s4/sources.yaml` (real, this imprint's actual watchlist including its own catalog URL); committed `sources.example.yaml` keeps the two universally-useful feeds and the Gutenberg mechanism as real, working values (genuinely good defaults for *any* public-domain-focused imprint), but genericizes the `catalog_reference` URLs to an obvious placeholder domain, since that section is inherently single-imprint-specific — same "committed example gets business-identity fields genericized" treatment `marketing_metadata.example.yaml` already got in ADR 002. Three source types, one per Decision point above:

```yaml
feeds:
  - name: valordecambio
    label: "Valor de Cambio — industria editorial en español"
    feed_url: https://valordecambio.com/feed/
    language: es
  - name: internet-archive-blog
    label: "Internet Archive Blog"
    feed_url: https://blog.archive.org/feed/
    language: en

gutenberg_catalog:
  catalog_url: https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv
  subject_keywords:
    - "Asia -- Description and travel"
    - "Central Asia"
    - "Mongolia"
    - "Tibet"
    - "Turkestan"
    - "Silk Road"
    - "Explorers"
    - "Voyages and travels"
    - "Description and travel"

catalog_reference:
  - name: ecosdeoriente-catalog
    label: "Catálogo actual de Ecos de Oriente"
    urls:
      - https://www.ecosdeoriente.com/portfolio/page/1/
      - https://www.ecosdeoriente.com/portfolio/page/2/
      - https://www.ecosdeoriente.com/portfolio/page/3/
```

The two feeds and the Gutenberg keyword list above are real, verified values, not placeholders — the point of writing this now is to give something to point at immediately, not a template to research yourself.

### 6. No scheduler inside the pipeline

Consistent with this project's existing "dumb and sequential... no background process, the filesystem is the record" design: `scan`/`briefing` are commands you run, not a service that runs itself. If you want it to happen automatically on a cadence, that's an OS-level scheduled task (cron / Windows Task Scheduler) calling `uv run python pipeline.py s4 scan && uv run python pipeline.py s4 briefing`, same as you'd schedule anything else — not a feature this codebase needs to own.

### 7. Gutenberg: the bulk catalog file, not the RSS feed — because the RSS feed is the wrong shape for a weekly cadence

Checked three options before picking one, since "guess and hope" is exactly what this project has learned not to do with external sources:

- `gutenberg.org/cache/epub/feeds/today.rss` — real, but scoped to "eBooks posted or updated in the last 24 hours." At a weekly cadence, running `scan` once a week against a 24-hour feed would silently miss ~6 days of new listings. Rejected — wrong window for the chosen cadence.
- Scraping the sorted search results (`/ebooks/search/?sort_order=release_date`) — no feed available (checked; no `<link rel="alternate">` in the page), would mean parsing paginated HTML with no publisher-sanctioned bulk-access path. Rejected — exactly the "scraping without a feed" case this ADR said should be a deliberate, case-by-case decision, and a better option exists.
- **`gutenberg.org/cache/epub/feeds/pg_catalog.csv`** — Gutenberg's own published bulk catalog dump, explicitly provided for automated/bulk use (this is their sanctioned mechanism, not a workaround). Columns: `Text#, Type, Issued, Title, Language, Authors, Subjects, LoCC, Bookshelves`. **Updated weekly** — a direct cadence match. Chosen.

Mechanically this is a different source *type* from an RSS feed, needing its own handling in `feed_scan`: download the CSV, filter rows where `Subjects`/`Bookshelves`/`LoCC` match a configured keyword list (Library of Congress subject headings for travel narratives commonly read like `"Asia -- Description and travel"`), then diff by `Text#` against `_state.yaml` exactly like an RSS item's GUID — the same dedup mechanism, just keyed on a different field. This also directly answers the user's actual question ("are new books in my genre showing up") far better than an unfiltered daily feed would have — filtering happens at the subject-heading level, not left to the LLM to guess relevance from noise.

Starter keyword list (tune once real output is seen): `Asia -- Description and travel`, `Central Asia`, `Mongolia`, `Tibet`, `Turkestan`, `Silk Road`, `Explorers`, `Voyages and travels`, `Description and travel`.

### 8. No System 1A database needed — the imprint's own live catalog page is the reference

`vsm.md`'s own example System 4 prompt asks about titles "not recently republished," which implies comparing against *something*. Rather than building a candidates database that doesn't exist yet (System 1A isn't implemented), the user pointed at their own site's real catalog: `ecosdeoriente.com/portfolio/page/{1,2,3}/` — three pages, ~8 published titles today, checked directly.

This works, and it's simpler than a database: since it's the user's own website, there's no scraping-ethics question at all, and the catalog is small enough (3 pages) to just **re-fetch fresh on every `briefing` run rather than diff for "new" items** — unlike the RSS/Gutenberg sources, this isn't about detecting change, it's a static reference block ("here is what we already publish") the briefing prompt uses to tell a genuine gap from something already in the catalogue. No state file needed for this source; no database; no System 1A dependency.

Mechanically this is a *third* source type in `feed_scan` (`catalog_reference`): fetch each configured URL, strip HTML to plain text with a small stdlib-only helper (`html.parser`, no new dependency — the content just needs to reach the LLM as readable text, not be precisely selector-extracted), and include the result in `combined.txt` inside a clearly-marked `<fuente>` block (see Decision point 10 for why this ended up as a tag, not a `=== HEADER ===` line) so `briefing`'s prompt can tell "this is what we publish" apart from "this is news to report on."

---

## Proposed briefing structure

Sections mapped directly to `vsm.md`'s System 4 responsibilities, so the output is legible against the framework it's meant to serve:

- **Panorama del sector** — what happened, grounded, dated, sourced
- **Tendencias emergentes** — patterns across multiple items, not single-article speculation
- **Riesgos** — legal, technological (AI), market-structure signals
- **Oportunidades** — anything suggesting catalogue whitespace, comparable-title activity, or a contact/relationship worth pursuing; this is where the `catalog_reference` block and any new Gutenberg matches get used — a new Gutenberg listing or a comparable-title mention is only actually an "opportunity" if the imprint doesn't already publish that author/work, which the prompt checks against the catalog reference before calling it one

Same discipline as every other prompt in this project by now: only report what's actually in the scanned material, cite the source article per claim (link back, the way `press-dossier`'s facts are grounded in `marketing_metadata.yaml` rather than invented), and no code-fence wrapping.

---

## Alternatives Considered

- **A true crawler** (follow links, discover sources) — rejected. Higher legal/ethical exposure for no real benefit at this scale; a 2–5 person imprint needs a stable watchlist of ~5–15 sources, not open-web discovery.
- **Scraping every source directly** (feed or no feed) — rejected for v1. Feeds already cover the two sources actually in hand; scraping without a feed is a per-source legal judgment call better made when a concrete feed-less source is actually wanted, not designed into the default path speculatively.
- **Cron/scheduling built into `pipeline.py`** — rejected, inconsistent with the project's explicit no-background-process design.
- **A three-stage map-reduce like `brief`'s chunking** (extract per-source, then synthesize) — rejected for v1 as premature: a handful of curated sources' new items in a week is a small enough volume for one synthesis call directly. Worth revisiting if the watchlist grows large enough that combined raw input starts exceeding a comfortable single-call size — same scaling question ADR 002 solved for manuscripts, not needed yet here.
- **Gutenberg's `today.rss`** — rejected, wrong time window for a weekly cadence (see Decision point 7).
- **Scraping Gutenberg's sorted search results** — rejected, no feed exists there and a better-fitting, publisher-sanctioned bulk mechanism (`pg_catalog.csv`) already exists.
- **A System 1A candidates database to support catalogue-gap detection** — rejected for now. The imprint's own live catalog page already answers "do we publish this," with no database to build or keep in sync (see Decision point 8).

---

## Consequences

**Easier:**
- `briefing` costs zero new code — same `llm_text.py`, a prompt file, a `tasks.yaml` entry.
- Adding a tenth, twentieth source to the watchlist is a `sources.yaml` edit, not a code change.
- `intelligence/`'s book-shaped structure means every existing convention (manifest tracking, `stage_output_dir`, gitignore pattern) applies without modification.

**Harder / needs care:**
- `scan` is the one genuinely new engine and the one new `task_loader.py` code path (`CLI_ARG = "none"`) — everything else in this ADR is reuse. It now has to handle three source shapes (RSS/Atom feed, a bulk CSV catalog, a small set of static reference pages) rather than one, which is real complexity worth keeping cleanly separated internally rather than one tangled fetch function.
- State tracking (`_state.yaml`, last-seen item per source) is new territory — nothing else in this project persists cross-run state outside a manifest. Needs to be designed to fail safely (a corrupted or missing state file should degrade to "treat everything as new" and warn, not crash).
- The Gutenberg subject-keyword list is a blunt instrument (substring matching against LCSH text) — it will both miss relevant new listings that happen to be catalogued under unexpected subject phrasing, and occasionally include false positives. Worth tuning once real output is seen, not treated as a solved filter.
- The briefing is only as good as the source list. A stale or narrow watchlist quietly produces stale or narrow intelligence with no error to signal it — worth an occasional manual sanity check ("does this briefing still feel useful?"), not something code can self-detect.

---

## Resolved decisions (2026-07-04)

1. **Cadence: weekly**, as defaulted — matches `valordecambio.com`'s posting rate, Gutenberg's catalog update rate, and `vsm.md`'s "2–3 hours/week" target. Run by hand for now.
2. **Starter watchlist expanded: add Gutenberg.** Not "another RSS source" — see Decision point 7 for why it needed its own source type (the bulk catalog CSV, not the 24-hour RSS feed) and how genre filtering works.
3. **Briefing language: Spanish**, confirmed, matching every other `s1d` prompt.
4. **No System 1A database — use the imprint's own live catalog page instead.** See Decision point 8. Simpler than what was proposed, and available immediately rather than blocked on 1A being built.

---

## Implementation notes (2026-07-04)

Built end-to-end against real sources, not simulated. Three findings that changed the design from what's written above:

### 9. Gutenberg's keyword list needs to be geographically specific, not just topically specific — found by actually running it

The first real `scan` run against the initial keyword list (`Description and travel`, `Explorers`, `Voyages and travels`, plus the geographic terms) returned **3,925 matches** — Mark Twain, L. Frank Baum's Oz books, R.L. Stevenson's Scottish travel essays, none of it Asia-related. Those three generic terms are common Library of Congress tags across *all* of Gutenberg's travel-adjacent catalog, for every country, not a genre filter at all. Removed them; kept only geographically-anchored phrases (`Central Asia`, `Tibet`, `Mongolia`, `Turkestan`, `Xinjiang`, `Silk Road`, `Pamir`, `Hindu Kush`, and `<Region> -- Description and travel` for Asia/Afghanistan/Persia specifically). Re-running against the corrected list returned real, on-target results — Sven Hedin's own "Im Herzen von Asien," Alexander Burnes's "Travels Into Bokhara" (Burnes is already in this imprint's catalog), Younghusband's "India and Tibet."

Added a second, independent safety net regardless of keyword quality: `feed_scan._scan_gutenberg` caps reported matches per run (`max_new_items`, default 50, configurable), keeping the most-recently-added (highest Text#) and noting how many were cut. This isn't a substitute for a well-tuned keyword list — it's a backstop so a future keyword misconfiguration (or the unavoidable cold-start flood covered next) can't flood `briefing`'s single LLM call again.

**Corollary — cold start is expected, not a bug:** with `_state.yaml` empty on a first run, every historically-matching row becomes "new" simultaneously, since nothing has been marked seen yet. This isn't specific to Gutenberg; it's inherent to any diff-based watchlist. After a first run, steady-state weekly scans only surface what's genuinely new that week.

### 10. `combined.txt`'s own section delimiters needed to be tags, not `=== HEADER ===` lines — the `brief` lesson recurring one level up

`briefing`'s prompt followed the fill-in-the-template style from ADR 002 from the start (literal headings, `[bracketed]` instructions below them) and still failed on first real use: headings came back either wrapped in a bracket pair (`## [¿Qué ha ocurrido en este período?]`) or freely paraphrased, non-deterministically, across three separate prompt-wording iterations (removing an inline date placeholder from the title; adding an explicit "no brackets in headings" rule; neither fully fixed it).

The actual fix was upstream of the prompt: `scan`'s own `combined.txt` delimited each source with `=== SOURCE LABEL (status) ===` — text that visually resembles a Markdown heading, which appears to have "primed" the model to keep inventing similarly-styled descriptive headings in its own output rather than reproducing the fixed template literally. Switching those delimiters to `<fuente nombre="..." estado="...">...</fuente>` tags (mechanically the same fix ADR 002 found for the `brief` task's own extraction contract, now recurring one layer up in a different task) removed the bracket-wrapping outright.

**What didn't fully resolve, and the call made about it:** heading *wording* still varies run to run (e.g. "Panorama del sector" vs. "Resumen de novedades en el período") even after the tag fix. Unlike `brief` (parsed by code, wording must be exact) or the `s1d` marketing documents (customer-facing, house style matters), `briefing` is read only by the person who ran it — a differently-worded but clear heading isn't a real defect. Chasing full literalness further would have been fighting non-determinism for a cosmetic property with no functional cost. Where the cost *is* real — a visible `[bracket]` artifact left in a finished document — see point 11.

### 11. A third generic `llm_text` normalizer: strip a heading entirely wrapped in one bracket pair

Same judgment call as the doubled-heading-marker fix already in `engines/llm_text.py`: once a defect is precisely, mechanically describable and prompt wording hasn't eliminated it after several honest attempts, normalize it in code rather than iterate indefinitely. `## [Heading text]` → `## Heading text` is safe to apply unconditionally to every `llm_text` task's output — no legitimate heading is ever entirely wrapped in one matching bracket pair. Added alongside the existing doubled-heading and code-fence normalizers in `_normalize_headings`/`_strip_code_fence`. This is now the third such normalizer; if a fourth distinct pattern shows up, that's a signal this class of Markdown-heading unreliability is common enough to warrant a shared, documented list of "known LLM heading artifacts" rather than three ad hoc regexes — not needed yet at three.

Also found and fixed in the same pass, unrelated to headings: `_strip_code_fence` in `llm_text.py` — the explicit "don't wrap in a code block" rule was already in every prompt post-ADR-002, and `briefing`'s response still came back ```` ```markdown ````-fenced on one run. Same principle: stripped in code, applied per-chunk so a fence around one chunk of a future multi-chunk response is still caught.

---

## Implementation Checklist

- [x] Add `feedparser` dependency (`uv add feedparser`) — robust RSS/Atom parsing across real-world feed variations, rather than hand-rolling XML parsing for a mechanism this central
- [x] Add `CLI_ARG = "none"` support to `lib/task_loader.py` (no positional argument; `root` resolved from `paths.intelligence_root(config)`, reading `config["intelligence_dir"]`, default `intelligence`)
- [x] Add `intelligence_dir: intelligence` to `config.example.yaml` (and the user's real `config.yaml`)
- [x] Create `engines/feed_scan.py` — three source-type handlers (RSS/Atom feed, Gutenberg catalog CSV, static catalog-reference pages), a shared `_state.yaml` diff mechanism for the first two, no diffing for the third; write per-source + `combined.txt` raw output (tag-delimited, see point 10); update state only on success; per-source fetch failures warn and continue rather than aborting the whole scan
- [x] Create `systems/s4/tasks.yaml` (`scan` → `engine: feed_scan`; `briefing` → `engine: llm_text`, `max_chars: 100000` so the combined scan is read in one call)
- [x] Create `systems/s4/sources.yaml` (real, gitignored) and `sources.example.yaml` (committed, `catalog_reference` genericized, Gutenberg keywords deliberately narrowed per point 9)
- [x] Write `prompts/examples/s4/briefing_task.txt` (English reference) + real Spanish prompt — needed three iterations beyond the ADR-002 baseline style before headings stopped showing bracket artifacts; see points 10–11
- [x] Add `intelligence/` and `systems/s4/sources.yaml` to `.gitignore`
- [x] Update README with the System 4 section
- [x] Add two new generic normalizers to `engines/llm_text.py` (bracket-wrapped heading, code-fence-per-chunk) — not originally scoped, found necessary during end-to-end testing, see point 11
- [x] End-to-end test: `s4 scan` against all three real source types (including a real mid-test transient 500 error from one feed, handled gracefully as designed) and `s4 briefing` against real scanned output, including the "quiet week, nothing new" case
