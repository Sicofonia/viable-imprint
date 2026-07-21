# ADR 011 — System 3: Sales Ingestion (Revenue and Reported Margin)

**Status:** Proposed. Not yet implemented — for review before work starts.

---

## Context

Second half of next-steps.md item 3, and the more structurally involved one:

> **Sales data ingestion:** the dashboard knows cost, not value — `vsm.md`'s first S3 metric (gross margin per title) is unanswerable. KDP and IngramSpark export royalty reports (CSV). Ingest as a new source type (a `feed_scan`-style CSV reader), store per book, surface margin per title in the dashboard. Manual download + drop-in of the CSV is fine for v1; no portal automation.

`s3 dashboard` (ADR 005) knows exactly what a book cost in API spend. It has no idea what it earned. **Explicitly raised by the user before design started here: this must not be hardcoded to one distribution platform.** This project already commits to being reusable by another imprint (the newsletter's tracking files, `sources.yaml`'s watchlist, and `marketing_metadata.yaml` are all gitignored-with-`.example` for exactly this reason) — a sales-ingestion feature that only understands IngramSpark's report format would break that commitment the moment a different imprint sells through KDP alone, or a future one adds Draft2Digital, direct sales, or a platform that doesn't exist yet.

**Two real gaps had to be resolved before "ingest a CSV" was even well-defined, not just the format question:**

1. **How does a sales report row (identified by ISBN) get matched back to a specific book folder?** Checked before designing anything: `templates/marketing_metadata.yaml` is *not* actually per-book despite reading that way — it's one fixed, gitignored, repo-root path (`metadata_config: templates/marketing_metadata.yaml`, referenced identically across every `s1d` task in `systems/s1d/tasks.yaml`), and its ISBN lives as a free-text `{label: "ISBN", value: "..."}` pair inside an arbitrarily-named block (`book_facts`) that a publisher could rename or omit entirely. There is no reliable, structured, genuinely-per-book place to read a book's ISBN from today. This has to be fixed as part of this ADR, not assumed away.
2. **Royalty reports are periodic, not one-shot.** A publisher downloads a new export every reporting period (monthly for KDP, quarterly for IngramSpark) for the life of a title. "Ingest a CSV" has to mean *accumulate*, not *replace* — the same shape the newsletter's tracking lists already solved (flat, append-only, deduped against what's already recorded), not the run-state ledger's shape (one entry per key, overwritten each time, ADR 004/009).

---

## Decision

### 1. Modularity: one small adapter per platform, auto-detected from the CSV's own header row

New `providers/sales/` package, mirroring the existing `providers/llm/` and `providers/translation/` shape — a common interface, one module per platform, nothing in the engine that names a specific platform:

```python
# providers/sales/base.py
class SalesFormat:
    """One subclass per distribution platform's royalty/sales CSV export.
    New platforms are added by writing one new module here and registering
    it below — the engine (`engines/sales_ingest.py`) never names a specific
    platform. See ADR 011.
    """
    name: str  # e.g. "ingramspark"

    @staticmethod
    def matches(header_row: list) -> bool:
        """True if this format recognizes the CSV's own header row — used
        for auto-detection, so a publisher can drop in whatever export they
        downloaded without saying which platform it's from."""
        raise NotImplementedError

    @staticmethod
    def parse(rows: list[dict]) -> list[dict]:
        """-> normalized rows: isbn, units, revenue, currency, period_start,
        period_end. Exact source column names are platform-specific and were
        NOT assumed in this design doc — real headers must be verified
        against an actual downloaded export during implementation (same
        discipline as ADR 005's pricing config: don't hardcode a fact that
        can silently go stale or simply be wrong)."""
        raise NotImplementedError
```

```python
# providers/sales/__init__.py
from . import ingramspark, kdp

FORMATS = [ingramspark.IngramSparkFormat, kdp.KDPFormat]

def detect(header_row: list) -> type[SalesFormat] | None:
    return next((f for f in FORMATS if f.matches(header_row)), None)
```

Two adapters ship — `ingramspark.py` and `kdp.py`, the two platforms the review named — but the point of this structure is that a third (Draft2Digital, direct sales, a platform that doesn't exist yet) is exactly one new small file, registered in one list, never touching `engines/sales_ingest.py`. This directly answers the user's feasibility question: yes, and the mechanism is the same one already proven for LLM/translation providers, just plural instead of singular (a publisher accumulates reports from however many platforms they actually sell through, not "pick one provider and use it for everything," which is why detection-per-file rather than one static `sales.provider:` config key is the right shape here — unlike `llm.provider`/`translation.provider`, this isn't a single standing choice).

An explicit `--format <name>` CLI override exists for the case detection fails or is ambiguous (a publisher's own export got hand-edited, or a platform changes its column layout) — auto-detection is the default path, not the only path.

### 2. The ISBN gap: a real, structured, per-book field — not parsed out of `marketing_metadata.yaml`

Add `isbn:` as a new flat key directly in each book's own `manifest.yaml` — the one place that's genuinely, unambiguously scoped to one specific book (unlike `marketing_metadata.yaml`). Set via a small new command, not hand-edited, matching this project's existing precedent of a tiny explicit command for a fact nobody should mistype (`pipeline.py candidate new <slug>`'s bootstrap shape):

```bash
pipeline.py book set-isbn life-as-explorer 978-1-0686007-2-2
```

This is deliberately separate from `init` (which runs before a title even has an ISBN assigned, typically well before `s1d metadata`) and deliberately separate from `marketing_metadata.yaml` (which stays exactly what it already is — free-form, human-facing marketing copy — not repurposed as a machine-read join key). `lib/manifest.py` gains `set_field(book_dir, key, value)` or simply reuses `update()` directly (`isbn` isn't a reserved/protected key the way `tasks`/`sales` are — see point 5).

### 3. Storage: an append-only per-book ledger, not an overwrite — `manifest.yaml`'s new `sales:` block

```yaml
# a book's manifest.yaml
isbn: 978-1-0686007-2-2
sales:
  - platform: ingramspark
    period_start: "2026-04-01"
    period_end: "2026-06-30"
    units: 34
    revenue: 142.50
    currency: USD
    ingested_at: "2026-07-21T09:03:11"
  - platform: kdp
    period_start: "2026-06-01"
    period_end: "2026-06-30"
    units: 12
    revenue: 38.20
    currency: USD
    ingested_at: "2026-07-21T09:04:02"
```

Deduped on `(platform, isbn, period_start, period_end)` before appending — re-ingesting the same export a second time (a publisher re-downloading a report they already processed) is a no-op, not double-counted revenue. This is structurally the newsletter's tracking-list pattern (ADR 008: flat, append-only, checked before appending), not the run-state ledger's pattern (ADR 004/009: one entry per key, overwritten) — sales data is cumulative history by nature, a task's completion status is not.

### 4. Where ingestion lives: System 3's first real task — and a real, named CLI wrinkle

`vsm.md` names "gross margin per title" as a System 3 metric (already quoted in ADR 005's Context) — sales data belongs to S3 conceptually, the same way ADR 005 already established. But S3 currently owns zero tasks; `pipeline.py s3 dashboard` is entirely hand-written, reading data other systems produced (ADR 005 point 6). This ADR gives S3 its first actual task, `sales-ingest`, declared in a new `systems/s3/tasks.yaml` — mechanical CSV capture, zero LLM, same "reporting infrastructure, not judgment" class as `feed_scan`/`homeostat_scan`, not a departure from S3's "reporting only" scope (point-quoted from ADR 005's Context: *"that's the one piece that's just code once the underlying data exists"* — this is still just code, now with one more data source to be code about).

**The real wrinkle:** every task `lib/task_loader.py`'s `_build_command()` generates today assumes a single positional file argument, with `root` resolved by walking *up* from that file to find the nearest `manifest.yaml` (`paths.book_root()`). That works because every existing book-scoped task's input is a file already living inside `books/<slug>/...`, produced by a previous task. A royalty CSV isn't — it's an external download (wherever the publisher's browser put it), with no book folder anywhere in its ancestry. `sales-ingest` needs the book named explicitly, the same two-argument shape System 2's own commands already use (`s2 run <book_slug>`, `book_slug` first, not derived from a path):

```bash
pipeline.py s3 sales-ingest life-as-explorer downloads/ingramspark-q2-2026.csv [--format ingramspark]
```

So `sales-ingest` is declared in `systems/s3/tasks.yaml` (for engine dispatch and so it flows through `task_loader.run_task()`'s shared ledger-recording hook exactly like every other task) but, unlike everything `build_system_group()` auto-generates, gets its own hand-written Click command in `pipeline.py` — the `_resolve_book_root()` helper `s2`'s commands already use, reused here rather than `paths.book_root()`'s directory-walk. `s3`'s command group itself changes shape to match: from a plain hand-written `@click.group` (today) to the same mixed pattern `s5` already uses (`_s5_group = build_system_group(...)`, then one hand-written command added to that same group variable, ADR 007 point 5) — except here, unusually, the *task-declared* command (`sales-ingest`) is the hand-written one, and it's `dashboard` that stays hand-written for the opposite reason (it was never task-shaped to begin with). Worth naming plainly rather than glossed over, same discipline ADR 008 used calling out its own book_scoped wrinkle explicitly.

`sales-ingest` is **not** added to `orchestrator.BOOK_SYSTEMS` or any book's `s2 run` graph — matching ADR 004 point 7's original reasoning (a periodic, externally-triggered, human-initiated action doesn't belong in the automated production chain), the same reasoning that already keeps System 4's scan and the homeostat/newsletter pipelines out of it.

### 5. Guarding `sales:` the same way `tasks:` is already guarded

ADR 009 added a runtime guard to `manifest.update()` rejecting a direct `tasks=` write, since that block must only ever be written through `record_task()`/`mark_stale()`. `sales:` needs the identical protection for the identical reason (accumulated by dedup-checked appends, never a blind overwrite) — extend the same guard:

```python
def update(book_dir: Path, **kwargs) -> None:
    for protected in ("tasks", "sales"):
        if protected in kwargs:
            raise ValueError(f"'{protected}' must be written via its own dedicated function, never update().")
    ...
```

### 6. `s3 dashboard`: revenue and *reported* margin, honestly labeled

A book with any `sales:` entries gets two new columns; a book with none shows `-`, exactly like cost already does when no pricing is configured (ADR 005's established optional-data convention). Revenue is **not currency-converted** — v1 deliberately doesn't invent an FX-rate mechanism (another "would silently go stale" risk, same reasoning ADR 005 rejected hardcoded pricing for); multi-currency revenue is shown grouped by currency (`$142.50, €38.20`), not blended into one falsely-precise number.

"Margin" here means **reported revenue minus tracked API cost only** — not `vsm.md`'s full gross-margin-per-title metric, which would also need design, freelance editorial time, ISBN fees, and print cost, none of which this project tracks (ADR 005 point 7 already drew this exact line for "API cost" vs. "full production cost"; this ADR draws the same honest line one level up, for margin). Labeled explicitly as "reported margin (API cost only)" in the dashboard output, not "margin," so it can't be mistaken for the real number by omission.

---

## Alternatives Considered

- **A single `sales.provider: ingramspark` config key, like `llm.provider`** — rejected (point 1): a publisher plausibly sells through more than one platform at once and needs to ingest reports from all of them over time, unlike LLM/translation where exactly one provider is genuinely in use per book. Auto-detection per file, with an explicit override, fits that shape; a static single-provider config doesn't.
- **Reading ISBN from `marketing_metadata.yaml`'s free-form blocks instead of adding a new `manifest.yaml` field** — rejected (point 2): that file isn't actually book-scoped today (one fixed repo-root path) and its labels are free text a publisher can rename — building a machine join key on top of human-facing marketing copy is fragile in exactly the way this project has avoided elsewhere (e.g. never asking an LLM to guess an ISBN in the first place, same instinct applied to *reading* one reliably).
- **Overwriting a per-book "latest sales snapshot" instead of an append-only ledger** — rejected (point 3): royalty reports are periodic; a snapshot model would lose every period except the last one ingested, making "gross margin per title" answerable only for whatever the most recent report happened to cover, not the book's life to date.
- **Automating the download from each platform's portal (API or scraped)** — explicitly out of scope, matching the review's own instruction ("manual download + drop-in of the CSV is fine for v1; no portal automation") and this project's standing avoidance of unattended account automation against third-party platform terms of service.
- **Automatic currency conversion via a live FX API** — rejected for v1 (point 6): adds a new external dependency and a "silently stale" risk class this project has deliberately avoided everywhere pricing-like data appears (ADR 005's `pricing:` block is user-maintained and un-auto-updated for the same reason). Grouped-by-currency display is honest and requires nothing external.
- **Folding `sales-ingest` into `s1d` instead of giving `s3` its first task** — considered: S1D already owns per-book marketing/financial-adjacent facts (`marketing_metadata.yaml`). Rejected because `vsm.md` explicitly names "gross margin per title" as an S3 metric, and mixing revenue-tracking into S1D would blur the same S1D/S3 line ADR 005 was written to draw in the first place (S1D produces marketing artifacts; S3 measures how the title is actually doing).

---

## Consequences

**Easier:**
- `vsm.md`'s first named System 3 metric — "gross margin per title" — goes from *unanswerable* to *reported, honestly labeled, and improving in accuracy as more cost categories are eventually tracked* (this ADR doesn't need to wait for those; it's additive).
- The modularity the user asked for is real, not aspirational: a third platform is one new file in `providers/sales/`, registered in one list — the same proof-of-concept `providers/llm/`/`providers/translation/` already are for this project's provider-swappability claims.
- `isbn:` becomes a real, structured per-book fact for the first time — useful beyond this ADR too (any future task that needs a book's ISBN reliably now has one place to read it from, instead of parsing free-form marketing copy).

**Harder / needs care:**
- Auto-detection is only as good as the two shipped adapters' `matches()` signatures — a platform that changes its export's column layout (both KDP and IngramSpark have done this historically, though the exact current layout needs verifying against real exports during implementation, not assumed here) could silently stop matching. The `--format` override exists for exactly this failure mode, but a publisher needs to notice detection failed rather than assume it silently worked; the engine should fail loud (a clear "couldn't recognize this CSV, pass --format" error) rather than guess.
- "Reported margin" is a narrower number than `vsm.md`'s real metric, same accepted-gap shape as ADR 005's "in-pipeline span" and "API cost" — worth remembering when reading the dashboard, not just when writing this ADR.
- `s3`'s command group changes shape (plain hand-written → mixed, like `s5`) — a small, precedented refactor, but worth a careful diff review so `dashboard`'s existing behavior isn't accidentally disturbed by the restructuring.
- Un-converted multi-currency revenue means a book selling meaningfully across currencies never gets one clean "total revenue" number — an accepted v1 limit (point 6), not an oversight.

---

## Implementation Checklist

- [ ] Add `providers/sales/base.py` (`SalesFormat` interface), `providers/sales/ingramspark.py`, `providers/sales/kdp.py` (real column mappings verified against actual downloaded exports, not assumed from this doc), `providers/sales/__init__.py` (`FORMATS`, `detect()`)
- [ ] Add `lib/manifest.py`: `set_isbn`-equivalent write path (or confirm plain `update()` suffices for `isbn`, since it's not a protected key); `record_sale(book_dir, entries)` — dedup on `(platform, isbn, period_start, period_end)`, append-only
- [ ] Extend `manifest.update()`'s ADR 009 guard to also reject a direct `sales=` kwarg (point 5)
- [ ] Add `engines/sales_ingest.py` (`CLI_ARG` irrelevant here since the CLI command is hand-written, not `_build_command()`-generated — see point 4): read CSV, `providers.sales.detect()` or explicit `--format`, normalize, filter to the book's own `isbn:`, dedup-append via `record_sale()`
- [ ] Add `systems/s3/tasks.yaml` declaring `sales-ingest` (engine + any default params)
- [ ] Add `pipeline.py book set-isbn <book_slug> <isbn>` command
- [ ] Convert `pipeline.py`'s `s3` group to the mixed `build_system_group()` + hand-written pattern (point 4); add the hand-written `s3 sales-ingest <book_slug> <csv_file> [--format]` command reusing `_resolve_book_root()`
- [ ] Extend `lib/dashboard.py`'s `book_summary()`/`portfolio_summary()` with revenue-by-currency and reported-margin fields; extend `pipeline.py`'s `s3 dashboard` rendering (both views) with the new, explicitly-labeled columns (point 6)
- [ ] End-to-end test: real or realistic sample CSVs in both IngramSpark's and KDP's actual export shapes (obtain or closely approximate real headers during implementation), confirm auto-detection picks the right adapter for each; confirm `--format` override works; confirm re-ingesting the same file twice doesn't double-count; confirm a CSV matching neither adapter fails loud with a clear message; confirm `s3 dashboard` shows revenue/margin only for books with sales data and `-` otherwise; confirm a direct `manifest.update(root, sales=[...])` call raises
- [ ] Update README (System 3 section, command reference table, a new "Record what a title actually earned" subsection in Running the Pipeline, and a note that `providers/sales/` is where a publisher adds support for another platform)
