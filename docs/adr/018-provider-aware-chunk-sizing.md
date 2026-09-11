# ADR 018 — Provider-Aware Chunk Sizing for `llm_text`

**Status:** Proposed — design only, no code changes in this PR. Implementation to follow as a separate pass once this design is confirmed, mirroring this project's own two-step precedent (a design-doc PR first, reviewed, then a separate implementation PR — used consistently since ADR 006; see also ADR 015→016's sequencing).

---

## Context

ADR 017's 2026-09-11 correction (see that ADR's "Correction (2026-09-11)" subsection and its second Implementation Notes section) found that Google AI Studio's real free tier is a flat **5 requests/minute / 20 requests/day (peak) / 250,000 input tokens/minute (peak)** across every model in the line — not the per-model-varying magnitude originally assumed. Tracing that forward found a concrete bug: `engines/llm_text.py`'s `max_chars` defaults to `8000`, a literal left over from the project's Mistral-only era, still silently in effect for `systems/s1b/tasks.yaml`'s `cleanup`, `ortho`, and `copyedit` — the three tasks that walk an entire manuscript, and so generate by far the most chunks (and requests) of any task in the pipeline. A follow-up fix (tracked separately, applied directly to `tasks.yaml`) raised those three tasks to `max_chars: 100000`, matching the value already proven safe for the 12 other `llm_text` tasks across `s1d`/`s4`/`s5`.

That fix closes the known instances. It does not close the underlying defect: **a hardcoded, provider-agnostic default baked into an engine's Python signature**, invisible until a manuscript-scale run against a rate-limited provider burns through a day's quota. Two things make this worth designing a general mechanism for, rather than treating the `tasks.yaml` fix as the end of the story:

1. **The defect can recur.** The next new task added to `s1b`/`s1d`/`s4`/`s5` without an explicit `max_chars` inherits the same stale `8000` default. The next provider or model swap (this project has already changed its default LLM provider twice — Mistral → Groq → Google AI Studio) can silently change what a *safe* chunk size actually is, with nothing in the code aware that it changed.
2. **The `100000` fix borrows safety it was never tested for.** The 12 `s1d`/`s4`/`s5` tasks that already used `max_chars: 100000` are compression/synthesis tasks — a `synopsis` or `press-dossier`'s output is much shorter than its input. `cleanup`/`ortho`/`copyedit` are roughly 1:1 transformation tasks — the model rewrites something close to the whole chunk it's given, so a 100,000-character input chunk can produce a comparably large output. Borrowing the `100000` figure from a different task shape is a reasonable emergency fix, not a designed-in guarantee against `finishReason=MAX_TOKENS`.

This ADR designs a mechanism that derives `max_chars` from the active provider/model's real output-token ceiling, so both task shapes are protected by construction instead of by a borrowed number. It is a design document only — no code changes ship with this PR.

---

## Decision

### 1. Capability metadata lives in a new `llm.limits` config block, mirroring `pricing:`

```yaml
llm:
  provider: google-aistudio
  model: gemini-3.6-flash
  temperature: 0.0
  thinking_level: low
  pricing:
    prompt_per_million: 0.75
    completion_per_million: 3.75
  limits:
    max_output_tokens: 65536
```

`config.example.yaml`'s `llm:` block already carries exactly this shape for `pricing` — a flat, single-model value (not a table keyed by model name), user-maintained, opt-in, with a config comment warning it needs to be re-confirmed against Google's current documentation. That last point is not hypothetical caution: it is the identical failure mode ADR 017 Decision 7 already named for pricing ("never hardcode prices, they go stale"), and this very ADR exists because an assumed rate-limit figure went stale and was wrong. `max_output_tokens` is the same category of externally-set, changeable fact.

A hardcoded per-provider constant (mirroring the existing retry-constant pattern in `providers/llm/google_aistudio.py`) was the original default recommendation during this ADR's design conversation, on the grounds that a config surface shouldn't be built ahead of a second real consumer (this project's own stated discipline: "a third provider is the trigger to extract [the retry loop], not the second" — ADR 017 Decision 5). The user's explicit direction, weighed against that, favors config: `pricing` already established that account/model-tier facts belong in user-editable config, not code, specifically because they drift — and a rate-limit figure has now visibly drifted twice in this project's short history (ADR 017's original `3.8`→`3.6` model swap, and the 2026-09-11 magnitude correction). This decision follows that precedent.

The `limits` block is **provider-agnostic in shape** (like `thinking_level`, which Mistral's provider simply ignores) — only the Google-AI-Studio-aware resolution logic in `engines/llm_text.py` (Decision 4) reads it today. `pricing` sets the precedent for this shape: a key that exists under `llm:` without a `provider:`-keyed nesting, understood to describe whatever model is currently configured.

### 2. Token-to-character conversion is a conservative hardcoded constant, not calibrated from history

```python
# engines/llm_text.py (illustrative — implementation PR decides exact placement)
_CHARS_PER_TOKEN_ESTIMATE = 3.5  # conservative: skews toward a smaller,
# safer max_chars rather than a larger, riskier one
```

`max_chars = int(llm.limits.max_output_tokens * _CHARS_PER_TOKEN_ESTIMATE)` — worst-case, task-shape-agnostic: it assumes a chunk's output could be as large as its input, which protects 1:1 transformation tasks (`cleanup`/`ortho`/`copyedit`) without needing the mechanism to know which task shape it's computing for.

The alternative considered — deriving or calibrating this ratio empirically from real `completion_tokens` already captured in every book's `manifest.yaml` (via `lib/metrics.py`'s `enrich()`) — was rejected for this pass. ADR 017 Decision 4 made a deliberate choice to fold `thoughtsTokenCount` into `completion_tokens` for cost-accuracy reasons ("thinking tokens are billed at the output rate... omitting it would silently under-report cost"). That choice means the captured number is not a clean measure of *output text length* — it conflates real output tokens with thinking tokens that bear no relationship to how long the visible output actually is. There is no way to recover a trustworthy chars-per-output-token ratio from data captured this way without adding new instrumentation (e.g. splitting `candidatesTokenCount` out separately), which this ADR deliberately does not propose. A fixed, conservative constant is the honest choice given what's actually measurable today.

### 3. Precedence: a task's explicit `max_chars` in `tasks.yaml` always wins outright

```python
# engines/llm_text.py — illustrative signature change
def run(input_file: Path, root: Path, system: str, output_name: str, config: dict,
        *, prompt: str, manifest_key: str = None, max_chars: int = None,
        temperature: float = None, metadata_config: str = None,
        metadata_footer: str = None, single_chunk: bool = False,
        reduce_prompt: str = None) -> Path:
    ...
    effective_max_chars = max_chars if max_chars is not None else _resolve_default_max_chars(config)
    chunks = chunk_by_paragraphs(raw_text, max_chars=effective_max_chars)
```

Not `min(explicit, provider-derived)`. The rejected alternative carries a real regression risk: six of ADR 014's twelve `single_chunk: true` tasks (`newsletter`, `article-draft`, both `s4` tasks, both remaining `s5` tasks) have **no** `reduce_prompt`. Those tasks work today, unconditionally, at `max_chars: 100000` — a deliberate, documented value (ADR 002/014). If a future provider or model's derived ceiling ever computed something smaller than `100000`, `min()` would silently start chunking books that work fine today, and — with no `reduce_prompt` configured — ADR 014's overflow guard would then hard-fail them: a behavioral regression on tasks nobody touched, triggered by an unrelated provider/model change. Explicit-in-YAML-always-wins avoids this class of regression entirely: an author who wrote `max_chars: 100000` made a decision this mechanism should never silently override; the new default only ever fills in for a task that specified nothing.

**A consequence worth stating plainly, not glossed over:** after the separate `tasks.yaml` fix (see Context) ships, *every* task currently in the repo — the 12 existing `s1d`/`s4`/`s5` tasks plus `cleanup`/`ortho`/`copyedit` — will have an explicit `max_chars`. This mechanism therefore has **zero effect on any task that exists today**. It is purely forward-looking: it protects the next task added without an override, and the next provider or model swap. That is the intended shape of this decision — a static, human-reviewed, urgent fix landed first (outside this ADR); this ADR's mechanism is the defense against the same class of bug recurring, not a second fix for the same instances.

### 4. Injection point: each engine resolves its own default internally

`lib/task_loader.py`'s `run_task()` needs **no changes**. It already forwards every non-reserved `tasks.yaml` key (`_RESERVED_KEYS = ("name", "engine", "output", "input", "book_scoped")`) straight into `engine.run(**extra_params)`, with no engine-specific knowledge — `max_chars` reaches `llm_text.run()` today only when a task sets it, which is exactly the seam Decision 3 relies on. Making `task_loader.py` engine-aware, so it could compute and inject a default before dispatch, was considered and rejected: it would break the loader's deliberately generic, engine-agnostic dispatch design for a need only one engine (`llm_text`) currently has.

`engines/translation.py`'s separately hardcoded `max_chars=50000` (DeepL) is explicitly **not** touched by this ADR (see Decision 7) — the same internal-resolution pattern would transfer cleanly to it in a future pass, but DeepL is a different provider family entirely, bound by a monthly character quota, not a requests-per-day rate limit, and needs its own analysis before this mechanism is assumed to apply.

### 5. No interaction with ADR 014's `single_chunk` overflow guard, by construction

Because every task that needs the guard (all 12 existing `single_chunk: true` tasks, plus `cleanup`/`ortho`/`copyedit` after the separate `tasks.yaml` fix) already sets `max_chars` explicitly, `_resolve_default_max_chars()` is never called for any of them (Decision 3). The implementation checklist below includes a one-time verification of this invariant — a repo-wide grep confirming zero `single_chunk: true` tasks lack an explicit `max_chars` — rather than new runtime enforcement machinery, matching this project's general preference for a documented, verified invariant over added code.

### 6. Proactive RPM pacing: deferred to a separate future ADR

Today, `providers/llm/google_aistudio.py`'s retry loop is purely reactive — it backs off only after a `429`. At a flat 5 RPM, proactive spacing between successful calls is a related idea (same "adapt to the provider's real limits" theme this ADR is part of), but it is a genuinely different mechanism: stateful call-pacing logic, not a pure default-value computation, and touches the retry loop this project has twice now deliberately kept simple and self-contained per provider (ADR 017 Decision 5). Bundling it here would materially widen this ADR's scope for a loosely-related concern. Deferred, with a named trigger for revisiting: if reactive backoff alone proves insufficiently smooth on a long chunked run once chunk counts are already reduced by this ADR's mechanism and the separate `tasks.yaml` fix.

### 7. Explicitly out of scope

- **RPD budget-visibility tracking** (a running "requests used today" counter or warning). Discussed and agreed separately as adjacent-but-related, not part of chunk sizing itself. If ever built, it would reuse `lib/dashboard.py`'s existing usage-ledger pattern (ADR 005/010) — every task run already records `provider`/`model`/`usage` per book, so no new instrumentation would be needed, only a new read/aggregation view.
- **`engines/translation.py`'s hardcoded `max_chars=50000` (DeepL).** A different provider family — bound by a monthly character quota (see the project's own DeepL-quota-split production note), not a per-day request count. Needs its own analysis, not an assumed extension of this ADR's mechanism.
- **Retroactively removing the 12+3 existing explicit `max_chars` overrides** in favor of always relying on the new dynamic default. A possible future cleanup once this mechanism has real track record; not part of landing it.

---

## Alternatives Considered

- **Hardcoded per-provider constants for `max_output_tokens`** (e.g. a `_MAX_OUTPUT_TOKENS` dict in `providers/llm/google_aistudio.py`), mirroring the existing retry-constant pattern. This project's own "don't build ahead of a second consumer" discipline argued for it; superseded by the user's explicit direction to use a config block instead (Decision 1), reasoning from the same "these facts drift" precedent already established for `pricing:`.
- **`min(explicit, provider-derived)` precedence.** Rejected: real regression risk on the six no-`reduce_prompt` `single_chunk` tasks if a future provider/model computes a smaller ceiling than today's (Decision 3).
- **Making `lib/task_loader.py` engine-aware** to compute and inject a default before dispatch. Rejected: breaks its deliberately generic dispatch design; the existing `extra_params` splat already provides the right seam, needed by only one engine today (Decision 4).
- **Empirical chars-per-token calibration from real `manifest.yaml` history.** Considered, not adopted: `completion_tokens` conflates real output text with thinking tokens (ADR 017 Decision 4), so no clean output-length signal exists in the data as currently captured (Decision 2).
- **Applying this ADR's dynamic default retroactively** to the 12 existing tasks and `cleanup`/`ortho`/`copyedit`, instead of leaving their explicit overrides in place. Rejected for this pass: trades a deliberately-authored, already-shipped value for a code-computed one with no equivalent validation history yet.
- **Building proactive RPM pacing into this same ADR.** Deferred, not rejected outright — a related but mechanically distinct concern (Decision 6).

---

## Consequences

**Easier:**

- A future task added without an explicit `max_chars`, or a future provider/model swap, inherits a provider-correct default automatically instead of silently repeating the exact bug ADR 017's 2026-09-11 correction found.
- The same mechanism protects both task shapes — compression/synthesis (`s1d`/`s4`/`s5`) and 1:1 transformation (`s1b`) — by construction, via the worst-case output-approximates-input assumption, rather than by borrowing a number proven for a different shape.
- Zero risk to any currently-explicit task (Decision 3) — this is a purely additive, forward-looking safety net.
- Small diff: one config block, one conservative constant, one changed default-parameter value in `engines/llm_text.py`, no change to `LLMProvider`'s interface or to `lib/task_loader.py`.

**Harder / needs care:**

- **Zero live effect immediately after landing**, since every task in the repo will already have an explicit `max_chars` by the time this mechanism could run. Worth stating plainly so this ADR isn't mistaken for fixing something still broken — the urgent instances are already fixed, separately, before this design exists.
- **`llm.limits.max_output_tokens` and `_CHARS_PER_TOKEN_ESTIMATE` are both unverified assumptions** at design time, needing a real-call check before being trusted — the same "confirm against a real account, not documentation" lesson this whole correction pair (ADR 017's magnitude error, this ADR's existence) is itself a case study in. Skipping that check here would be ironic.
- **A real finding from validating the separate `tasks.yaml` fix, directly relevant to this ADR's mechanism:** the real call made to confirm `max_chars: 100000` was safe for `cleanup` (see ADR 017's 2026-09-11 Implementation Notes) succeeded — no `MAX_TOKENS`, output came back almost exactly 1:1 with input as expected — but took 359 seconds and needed two retries, because `providers/llm/google_aistudio.py`'s `complete()` sends every request with a **fixed** `timeout=120.0` that does not scale with request or expected-response size. A large `max_chars` value avoids `MAX_TOKENS` but can trade it for a different, less obvious risk: a structurally slow call that times out repeatedly against a size-unaware client timeout, each retried attempt very plausibly still costing one of the day's 20 requests even though the client never sees the response. **This ADR's mechanism (deriving `max_chars` from `max_output_tokens`) does not address this** — it only protects against `MAX_TOKENS`, not against latency/timeout risk, which is a function of expected response time, not token count alone. Named here as an open question for whoever implements this ADR to weigh, not solved by this design: either `providers/llm/google_aistudio.py`'s fixed timeout should itself become size-aware (a small, separate, provider-specific change, not part of this ADR's `llm.limits` mechanism), or `max_output_tokens`/the chars-per-token constant should be chosen conservatively enough that a request rarely approaches the point where this becomes likely. Not blocking this design, but should not be silently forgotten either.
- **A second place to look for a task's real `max_chars`** now exists — `tasks.yaml` (explicit) or engine-computed (implicit) — mitigated by echoing the resolved value in `llm_text`'s existing per-chunk progress line, so a real run always shows what was actually used.
- **Mistral is explicitly excluded** — it keeps its legacy `8000` literal via `_resolve_default_max_chars()`'s fallback branch. "Provider-aware" in this ADR's title means "Google-AI-Studio-aware" in practice; extending it to Mistral (or a future provider) is a natural, cheap follow-up once that provider also defines an `llm.limits` block, not something this ADR needs to solve now.

---

## Implementation Checklist

*(First draft, describing the scope of a later, separate implementation PR — nothing here ships with this design-only ADR.)*

- [ ] Add `llm.limits.max_output_tokens` to `config.yaml` and `config.example.yaml`, with a config comment on re-confirming the value against Google's current model documentation before trusting it (same caution already applied to `pricing`)
- [ ] Add `_CHARS_PER_TOKEN_ESTIMATE` and a `_resolve_default_max_chars(config)` helper to `engines/llm_text.py`; change `run()`'s `max_chars` parameter default from `8000` to `None`
- [ ] Grep-verify the zero-regression invariant (Decision 5): every `single_chunk: true` task, and `cleanup`/`ortho`/`copyedit`, already set `max_chars` explicitly
- [ ] Echo the resolved `max_chars` value in `llm_text.run()`'s per-chunk progress output
- [ ] One real-call validation against Google AI Studio, using a task with no explicit `max_chars` override, confirming the resolved default is sane and doesn't immediately trip `finishReason=MAX_TOKENS`
- [ ] Confirm a Mistral-configured run is unaffected (legacy `8000` literal preserved via the fallback branch)
- [ ] Update the README's `llm_text` engine description, scoped explicitly to what's Google-AI-Studio-specific vs. provider-general
- [ ] Explicitly not built in that pass: proactive RPM pacing (Decision 6), RPD budget visibility, `engines/translation.py` changes (Decision 7)
