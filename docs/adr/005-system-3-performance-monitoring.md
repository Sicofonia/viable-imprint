# ADR 005 — System 3: Performance Monitoring (Metrics Capture + Dashboard)

**Status:** Implemented. Built and tested end-to-end against a real (disposable) scratch book — see "Implementation notes" for two display bugs found and fixed while looking at real captured numbers.

---

## Context

The private strategic assessment that motivated ADR 004 named the measurement channel as the next step after System 2, in the assessment's own words: *"every engine already returns a path; make it also append duration, token cost, and model to a per-book log. S3 dashboards then cost almost nothing and give you the evidence base for closing review loops."*

`docs/vsm.md` gives System 3 five responsibilities: resource allocation, internal negotiation, **performance monitoring** ("review metrics from each unit and detect deviations from the plan"), operational financial management, and operational decision-making (print runs, pricing, reprints). The user was explicit, discussing this ADR: the first, second, fourth, and fifth of those stay human — they're genuine judgment calls an Editorial Director makes, not things a script should decide. Only **performance monitoring** is a reporting function: read what System 1's tasks (and System 2's run-state ledger, ADR 004) already produced, surface it, flag deviations. That's the one piece that's just code once the underlying data exists.

The user also asked for this as one tracer-bullet ADR rather than "capture metrics" now and "build something that reads them" as an unscoped future task — a measurement channel nobody looks at isn't actually a measurement channel. So this ADR covers both: extending the ADR 004 ledger with per-task metrics, and a minimal, read-only `s3 dashboard` command that reports against `vsm.md`'s own named System 3 metrics.

**Explicitly out of scope:** resource allocation, internal negotiation, budget tracking (there's no budget figure anywhere in this codebase to track a deviation against), pricing/reprint decisions, and anything that *acts* on the numbers rather than reporting them. This ADR adds a read-only mirror, nothing more.

---

## Decision

### 1. What gets captured, and where the data actually comes from

Checked before designing anything (not assumed):

- **Duration** — free. `lib/task_loader.py`'s `_run_recorded()` (ADR 004) already wraps every task's execution in one place, regardless of engine; wrapping that call in a timer covers every task, including the three engines that never touch a provider at all (`odt_format`, `metadata_doc`, `feed_scan`).
- **Tokens** — `providers/llm/mistral.py` calls the Mistral API directly over `httpx` (no SDK) and currently discards the response's `usage: {prompt_tokens, completion_tokens, total_tokens}` object after pulling out just the message text. The data is already in every response; it's just being thrown away.
- **Characters** — `providers/translation/deepl.py` uses the official `deepl` SDK; the `TextResult` object `translate_text()` returns carries `billed_characters`, also currently discarded after `.text` is extracted.
- **Cost** — **not available anywhere.** Neither provider's response nor `config.yaml` contains a price. Computing a dollar figure requires a new, explicitly user-maintained pricing table — model/character prices change over time and by account tier, so this can't be a hardcoded constant without going stale silently.
- **Model** — already known; `config["llm"]["model"]` is passed into every LLM-driven engine call today. No new capture needed, just recording it in the new ledger fields instead of only the legacy flat `llm_model` key.

Only two of the five engines call an external provider more than zero times (`llm_text`, `translation` — both loop one provider call per manuscript chunk); `odt_format`, `metadata_doc`, and `feed_scan` make no LLM/translation calls at all. The design below reflects that: duration is captured uniformly for all five; tokens/characters/cost are only ever present on the two that can produce them.

### 2. Provider interface: accumulate usage on the instance, don't widen `complete()`'s return type

Each engine that calls a provider creates exactly one provider instance per task run and loops every chunk's call through that same instance (`llm = get_llm_provider(config)` once, then `llm.complete(...)` per chunk). Rather than changing `complete()`/`translate()` to return a richer type — which would force every call site, including every future one, to unpack a tuple it usually doesn't care about — the provider accumulates usage on itself across calls, and the engine reads the total once after its loop finishes:

```python
# providers/llm/mistral.py
class MistralProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "mistral-medium-latest", temperature: float = 0.0):
        self._api_key = api_key
        self._model = model
        self._temperature = temperature
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def complete(self, system_prompt: str, user_prompt: str, temperature: float = None) -> str:
        response = httpx.post(...)
        response.raise_for_status()
        data = response.json()
        usage = data.get("usage", {})
        for field in self.usage:
            self.usage[field] += usage.get(field, 0)
        return data["choices"][0]["message"]["content"]
```

```python
# providers/translation/deepl.py
class DeepLProvider(TranslationProvider):
    def __init__(self, api_key: str):
        self._translator = deepl.Translator(api_key)
        self.usage = {"characters": 0}

    def translate(self, text, source_lang, target_lang) -> str:
        result = self._translator.translate_text(...)
        self.usage["characters"] += result.billed_characters
        ...
        return self._from_xml(translated)
```

`complete()`/`translate()` keep returning plain `str`, so every existing call site is untouched. `usage` is an informal convention documented on each abstract base class (`LLMProvider`/`TranslationProvider`), not an `@abstractmethod` — a provider that doesn't set it simply produces no usage data, handled as graceful degradation downstream (point 4), not a crash.

### 3. Engines report raw usage; `task_loader.py` turns it into ledger fields

`engines/llm_text.py` and `engines/translation.py` each gain one line at the end of `run()`: instead of `return output_file`, they return `output_file, {"usage": llm.usage}` / `output_file, {"usage": translator.usage}`. `odt_format.py`, `metadata_doc.py`, and `feed_scan.py` are unchanged — they keep returning a bare `Path`.

`lib/task_loader.py`'s `_run_recorded()` (ADR 004) is extended to accept both shapes and to time the call:

```python
def _run_recorded(run_fn, root, system, name, config):
    key = f"{system}.{name}"
    start = time.monotonic()
    try:
        result = run_fn()
    except Exception as e:
        manifest.record_task(root, key, status="failed", error=str(e),
                              attempted_at=_now_iso(),
                              duration_seconds=round(time.monotonic() - start, 2))
        raise
    duration = round(time.monotonic() - start, 2)
    output_file, raw_metrics = result if isinstance(result, tuple) else (result, {})
    manifest.record_task(
        root, key, status="done",
        output=str(output_file.relative_to(root)), completed_at=_now_iso(),
        duration_seconds=duration, **metrics.enrich(raw_metrics, config),
    )
    return output_file
```

New `lib/metrics.py` turns raw usage into ledger-ready fields, deriving `provider`/`model`/`cost_usd` from `config` (already available in `task_loader.py` — no need for engines to know or report pricing):

```python
def enrich(raw_metrics: dict, config: dict) -> dict:
    usage = raw_metrics.get("usage")
    if not usage:
        return {}
    if "total_tokens" in usage:
        return {
            "provider": config["llm"]["provider"],
            "model": config["llm"].get("model"),
            "usage": usage,
            "cost_usd": _llm_cost(usage, config["llm"].get("pricing")),
        }
    return {  # character-based usage (translation)
        "provider": config["translation"]["provider"],
        "usage": usage,
        "cost_usd": _translation_cost(usage, config["translation"].get("pricing")),
    }
```

`_llm_cost`/`_translation_cost` return `None` when no `pricing:` block is configured — cost is opt-in; duration and raw usage are captured regardless.

### 4. Pricing lives in `config.yaml` (gitignored), not in code

Prices change over time and by account tier — hardcoding a dollar figure per model name would silently go stale. Consistent with how this project already treats other mutable, personal facts (`marketing_metadata.yaml`, `format_styles.yaml`: real values gitignored, `.example` committed with placeholders), pricing is an optional block in `config.yaml`:

```yaml
llm:
  provider: mistral
  model: mistral-medium-latest
  temperature: 0.0
  # Optional. Omit to skip cost calculation — duration and token counts are
  # still captured either way. Verify current figures with your provider;
  # these are not auto-updated.
  pricing:
    prompt_per_million: 0.40
    completion_per_million: 2.00

translation:
  provider: deepl
  source_lang: EN
  target_lang: ES
  # Optional, same as above.
  pricing:
    per_million_characters: 20.00
```

`config.example.yaml` gets these keys with clearly-labeled placeholder numbers and a comment to verify against current pricing before relying on the dashboard's cost figures.

### 5. Resulting ledger shape (ADR 004's `tasks:` block, additive fields only)

```yaml
tasks:
  s1b.cleanup:
    status: done
    output: s1b/cleaned/zayagan-chp1.txt
    completed_at: "2026-07-10T09:12:03"
    duration_seconds: 12.4
    provider: mistral
    model: mistral-medium-latest
    usage: {prompt_tokens: 1840, completion_tokens: 910, total_tokens: 2750}
    cost_usd: 0.003
  s1b.translate:
    status: done
    output: s1b/translated/es/zayagan-chp1.txt
    completed_at: "2026-07-10T09:15:47"
    duration_seconds: 3.1
    provider: deepl
    usage: {characters: 8420}
    cost_usd: null   # no translation.pricing configured
  s1b.format:
    status: done
    output: s1b/formatted/zayagan-chp1.odt
    completed_at: "2026-07-10T09:17:02"
    duration_seconds: 0.4
    # no provider/model/usage/cost_usd — odt_format makes no external call
```

No schema migration needed for existing ledger entries (from books that already used `s2 run` before this ADR) — the new fields are additive; an old entry without them just doesn't contribute to cost/token totals, exactly like a task that used a no-usage engine.

### 6. `pipeline.py s3 dashboard [book_slug]`

A new, hand-written command group (like `s2` — System 3 doesn't declare tasks either). Read-only, no flags that change state.

**Portfolio view** (`s3 dashboard`, no argument) — one row per book under `books_dir`:

```
Portfolio: 3 books

Book              Tasks done   API cost   Compute time   In-pipeline span
life-as-explorer  13/13        $0.42      4m 12s         2026-07-02 -> 2026-07-05 (3 days)
zayagan-chp1      9/13         $0.31      3m 05s         2026-06-20 -> 2026-07-10 (20 days)
new-title         2/13         $0.02      0m 18s         2026-07-09 -> 2026-07-09 (in progress)

Totals: $0.75 API cost, 24 tasks completed, avg $0.25/title, avg compute time 2m 32s/title
```

**Per-book view** (`s3 dashboard life-as-explorer`) — one row per task, mirroring `s2 status`'s layout but with the metrics columns instead of status:

```
Book: life-as-explorer

Task                    Duration   Provider/Model            Usage                  Cost
s1b.cleanup              12.4s     mistral / mistral-medium   2,750 tokens           $0.003
s1b.translate              3.1s    deepl                      8,420 characters       -
s1b.copyedit               9.8s    mistral / mistral-medium   2,410 tokens           $0.003
s1b.format                 0.4s    -                          -                      -
...

Total: 4m 12s compute time, $0.42 API cost (translation cost omitted — no translation.pricing configured)
```

Both views are built on `lib/dashboard.py`: `book_summary(root) -> BookSummary` (reads one book's ledger, sums duration/cost, finds earliest/latest `completed_at`) and `portfolio_summary(books_dir) -> list[BookSummary]` (one per book folder with a `manifest.yaml`).

### 7. "In-pipeline span" is a labeled proxy, not `vsm.md`'s literal cycle-time metric

`vsm.md` names "average time of the complete cycle: text identified → book published" as a System 3 metric. Neither endpoint is instrumented today — System 1A (text identification) doesn't exist yet, and System 1D's actual print-on-demand portal upload isn't a tracked pipeline task (it's an external, manual action on KDP/IngramSpark). The dashboard's "in-pipeline span" (earliest `completed_at` to latest `completed_at` across a book's ledger) measures something narrower and different: how long a manuscript has been moving through *this pipeline*, not the true acquisition-to-shelf cycle. It's reported under its own honest label, not as the `vsm.md` metric it resembles — closing that gap for real is a System 1A problem, not something this ADR can manufacture data for.

Likewise, "average production cost per title" in `vsm.md` implies design, freelance editorial time, ISBN fees, and print costs alongside API spend — none of those are tracked here. The dashboard reports **API cost** specifically (LLM + translation spend), a real but partial subset of the full metric `vsm.md` describes.

### 8. System 4 gets the same capture mechanism for free, but isn't in the dashboard

`_run_recorded()` is generic across every system, so `s4 scan`/`s4 briefing` runs will also get `duration_seconds`/`usage`/`cost_usd` recorded into `intelligence/manifest.yaml`'s ledger automatically — no extra work. But `s3 dashboard`'s aggregation stays scoped to `books_dir` only, matching `vsm.md`'s System 3 being about *title* economics, not intelligence-scanning cost, and matching ADR 004's own precedent of scoping System 2's orchestrator to book-scoped systems only (point 7 there).

---

## Alternatives Considered

- **Widen `complete()`/`translate()` to return `(text, usage)` tuples** — rejected: forces every call site, including any future one that doesn't care about usage, to unpack a tuple. Instance-level accumulation (point 2) is smaller and the provider is already a single-use-per-task-run object, so there's no lifecycle risk to the mutable state.
- **A uniform `TaskResult(output, metrics)` return type across all five engines** — rejected: three of the five never produce metrics; forcing them to return `(path, {})` for no reason is unneeded churn for something the duck-typed tuple-or-Path check in `_run_recorded()` already handles cleanly.
- **Hardcoded pricing table in code, keyed by model name** — rejected: prices change over time and by account tier; a constant would go stale with no signal. Config-driven, optional, gitignored (point 4) matches how this project already treats other mutable personal facts.
- **A separate `metrics.yaml` file instead of extending `tasks:`** — rejected, same reasoning as ADR 004 point 2: one source of truth per book; a second file inviting drift buys nothing.
- **Claiming "in-pipeline span" is the full `vsm.md` cycle-time metric** — rejected (point 7): would be quietly wrong once System 1A and real POD-publication tracking exist; better to report a narrower, honestly-labeled number now than a falsely-precise one.
- **Rolling resource allocation, budget tracking, or pricing/reprint decisions into this ADR** — explicitly rejected per the user's direction: those stay human judgment calls; this ADR is performance monitoring only, matching `vsm.md`'s own split of System 3's five responsibilities.
- **A live/auto-refreshing dashboard (web UI or TUI)** — rejected: overkill for occasional review at a 2–5 person scale; a CLI table fits this project's existing "dumb and sequential" shape exactly as well as `s2 status` already does.

---

## Consequences

**Easier:**
- "What has this book cost so far, and how much of that was API spend vs. sitting idle waiting on review?" is answerable in one command instead of opening YAML files by hand.
- Once more than one book has run through the pipeline, the portfolio view gives the first real evidence base for `vsm.md`'s "detect deviations from the plan" — a title costing 3x the portfolio average per task becomes visible instead of merely felt.
- The assessment's remaining steps (an S5 policy agent, then System 1C) can proceed without this being revisited — metrics capture was the one piece they were implicitly waiting on.

**Harder / needs care:**
- Cost figures are only as trustworthy as the pricing config a publisher keeps up to date — there's no staleness check, no "last verified" date. A publisher who never fills in `pricing:` gets accurate tokens/duration and a `null` cost, which is honest but easy to forget is happening.
- "In-pipeline span" undercounts the true acquisition-to-shelf cycle (point 7) — worth remembering when reading the dashboard, not just when writing this ADR.
- A future provider that doesn't expose usage data at all (hypothetically, if a different LLM vendor were added) silently contributes no cost/token figures rather than erroring — correct behaviour by design, but means dashboard coverage is provider-dependent, not guaranteed.
- `_run_recorded()` picking up a `config` parameter (needed for `metrics.enrich()`) is a small signature change shared with `run_task()`'s existing call sites — low risk, but worth a careful read of both call sites (`_build_command`'s two callbacks, `orchestrator.run_book()`) during implementation.

---

## Implementation notes (2026-07-10)

Built end-to-end against a disposable scratch book (`books/s3-scratch-test/`, a two-sentence dummy manuscript, created and deleted within this same session — not part of the repo), running the real `cleanup`/`translate`/`ortho`/`copyedit` chain via `s2 run --only s1b` (real Mistral + DeepL API calls). Two display bugs found by looking at real numbers, neither a capture-correctness problem:

### 9. Per-task cost rounded to `$0.000` for small texts — the display, not the underlying data, was wrong

Real captured `cost_usd` values for a two-sentence manuscript were genuinely tiny (`0.000346`, `0.000252`, `0.000328`, `0.00412`) — correct, sub-cent numbers for ~600–700 tokens of Mistral output. `_format_cost()` as originally written used a fixed `.3f`, which rendered every one of them as `$0.000` — technically accurate rounding, but indistinguishable from "no cost data" and useless for verifying the feature actually worked. Fixed with adaptive precision: values under $0.01 print with 6 decimals (`$0.000346`), values at or above print with 3 (`$0.423`) — real books with real chapter-length chunks will mostly land in the second case; short test fixtures land in the first, and now show real signal either way.

### 10. Fixed-width column formatting silently glued adjacent columns together when content overflowed

`"mistral / mistral-medium-latest"` (32 characters) is wider than the `Provider/Model` column's original `:<26` spec — Python's format spec doesn't truncate or force a minimum gap, it just stops padding once the content is already longer, so the next column's text started immediately with zero separation (`mistral-medium-latest684 tokens`, no space). Fixed by widening the column to `:<32` and — more importantly — adding an explicit literal two-space separator between every column regardless of padding, so even a future overflow (a longer model name, a longer provider/model combination) degrades to a misaligned-but-readable row instead of visually merged, unparseable text. Applied to both the per-book table and the portfolio table.

Everything else matched the design as drafted: `duration_seconds` (including on the one deliberately-triggered failure, `s1b.format` — same pre-existing local template-path mismatch noted in ADR 004's implementation notes, unrelated to this ADR), `provider`/`model`/`usage`/`cost_usd` for the two provider-calling tasks, and no spurious metrics fields on the zero-provider failure. `s3 dashboard` (both views) rendered correctly against real data on the first try once the two display fixes above landed.

---

## Implementation Checklist

- [x] `providers/llm/base.py` / `mistral.py`: add an instance-level `usage` dict, accumulated across calls; document the convention on the abstract base class
- [x] `providers/translation/base.py` / `deepl.py`: same, using `billed_characters` from `TextResult`
- [x] `engines/llm_text.py` / `engines/translation.py`: return `(output_file, {"usage": provider.usage})` instead of a bare `Path`; `odt_format.py`, `metadata_doc.py`, `feed_scan.py` unchanged
- [x] Add `lib/metrics.py`: `enrich(raw_metrics, config) -> dict` plus `_llm_cost`/`_translation_cost` helpers, `None` cost when no pricing configured
- [x] Update `lib/task_loader.py`'s `_run_recorded()` to time each run (success and failure paths) and unpack the tuple-or-Path return shape; thread `config` through from `run_task()`
- [x] Add optional `pricing:` blocks to `config.example.yaml` under `llm:` and `translation:`, placeholder values, comment to verify current pricing (and to the user's real, gitignored `config.yaml`, so end-to-end testing could exercise real cost calculation)
- [x] Add `lib/dashboard.py`: `book_summary(root)`, `portfolio_summary(books_dir)`
- [x] Add the `s3` command group to `pipeline.py`: `dashboard [book_slug]`
- [x] Update README with the System 3 section (Architecture, the book-folder `manifest.yaml` description, a new "Check what it cost" subsection in Running the Pipeline, and an optional-pricing note in Setup)
- [x] End-to-end test: ran the real `s1b` chain against a scratch book via `s2 run --only s1b`; confirmed `duration_seconds`/`usage`/`cost_usd` landed correctly in the ledger for both LLM and translation tasks; confirmed the failed `format` task recorded `duration_seconds` with no spurious usage/cost fields; confirmed `s3 dashboard` (both portfolio and per-book views) renders correctly with real captured data — found and fixed the point-9 and point-10 display bugs in this pass; scratch book deleted afterward, not committed
