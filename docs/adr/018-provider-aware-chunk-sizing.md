# ADR 018 — Provider-Aware Chunk Sizing for `llm_text`

**Status:** Implemented (2026-09-11). Built per this document's own Decision points, with one honestly-flagged gap — see Implementation Notes: the computed default (229,376 characters against the real configured model) was validated for correct wiring against a small real call, but not stress-tested at its own actual size the way ADR 017's 2026-09-11 correction stress-tested `100000`, since doing so would cost meaningfully more of the scarce daily request budget for a value with real margin already reasoned through below. **Amended before merge** after the user caught, during pre-merge review, that `cleanup`/`ortho`/`copyedit`'s static `max_chars: 100000` (left over from ADR 017's correction) defeated this ADR's own purpose for those three tasks — an explicit `tasks.yaml` value always wins, so a Mistral switch would never have reverted them to a Mistral-safe size. Fixed by removing that static value so they use this ADR's mechanism too — see the last two Implementation Notes entries.

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

- [x] Add `llm.limits.max_output_tokens` to `config.yaml` and `config.example.yaml`, with a config comment on re-confirming the value against Google's current model documentation before trusting it (same caution already applied to `pricing`) — `65536`, confirmed against Google DeepMind's own model card (2026-09-11), not a secondary source
- [x] Add `_CHARS_PER_TOKEN_ESTIMATE` and a `_resolve_default_max_chars(config)` helper to `engines/llm_text.py`; change `run()`'s `max_chars` parameter default from `8000` to `None`
- [x] Grep-verify the zero-regression invariant (Decision 5): every `single_chunk: true` task, and `cleanup`/`ortho`/`copyedit`, already set `max_chars` explicitly — confirmed, 12 `single_chunk: true` entries and 15 `max_chars` entries (12 + the 3 from `s1b`) line up 1:1
- [x] Echo the resolved `max_chars` value in `llm_text.run()`'s per-chunk progress output
- [x] One real-call validation against Google AI Studio — see Implementation Notes for why this validated correct wiring, not the computed value's size safety, and why that gap is acceptable for now
- [x] Confirm a Mistral-configured run is unaffected (legacy `8000` literal preserved via the fallback branch) — confirmed directly against `_resolve_default_max_chars()` with a Mistral-shaped config lacking `llm.limits`
- [x] Update the README's `llm_text` engine description, scoped explicitly to what's Google-AI-Studio-specific vs. provider-general
- [x] Explicitly not built in this pass: proactive RPM pacing (Decision 6), RPD budget visibility, `engines/translation.py` changes (Decision 7)

---

## Implementation Notes (2026-09-11)

Built directly against this document's own Decision points, on a fresh branch off `main` after PR #36 (ADR 017's correction) had already merged.

**`max_output_tokens: 65536` was sourced, not assumed.** A web search first suggested this figure via secondary sources (blog posts), which this project has been burned by trusting once already (ADR 017's original RPD figure). Cross-checked against Google DeepMind's own model card for `gemini-3.6-flash` (`deepmind.google/models/model-cards/gemini-3-6-flash/`), which states the same number directly: "64K token output." Used as the config value with that provenance recorded in a comment, per Decision 1's own caution.

**The computed default resolves to 229,376 characters** against the real, currently configured model (`65536 × 3.5`) — over twice `100000`, the value ADR 017's correction validated directly. Worth being honest about what was and was not re-proven here:

- **Wiring correctness was validated with a real call.** `_resolve_default_max_chars()` was exercised end-to-end — real config, real provider, real API call — against the same small test fixture ADR 017's own validation used (`books/test/s1b/source/zayagan-chp1.txt`, 15,101 characters), run in a throwaway scratch book and cleaned up after. The new progress line correctly printed `max_chars=229376 (1 chunk)`, the call completed cleanly, and the output was well-formed. This confirms the mechanism reads config, computes the right value, and reaches the provider correctly.
- **Size safety at 229,376 characters specifically was not re-proven with a large real call**, unlike `100000`'s validation in ADR 017's correction. The small fixture is well under the computed ceiling, so it never actually produced a chunk anywhere near that size. Deliberately not spending a second large chunk's worth of the day's 20-request budget to re-prove a number this document can already reason about with real data: ADR 017's own validation of the real production manuscript at `100000` characters recorded `completion_tokens: 22660` for an output of `97,446` characters — a real, observed output efficiency of ~4.30 characters per token, noticeably better than this document's conservative `3.5` estimate. Scaling that real ratio to `229,376` characters of comparably-shaped 1:1 output predicts roughly `53,300` output tokens — about 81% of the `65,536` ceiling, leaving real (if not huge) margin. This is reasoning from real data, not a fresh guess, but it is extrapolation, not a direct measurement at this size — named honestly rather than presented as equivalent to the `100000` validation.
- **If this margin ever turns out to be too thin in real production use** (a `finishReason=MAX_TOKENS` on a task relying on the computed default), the fix is a config change, not a code change: lower `chars_per_token`'s effective conservatism by editing `_CHARS_PER_TOKEN_ESTIMATE`, or set an explicit, smaller `max_chars` directly on the affected task in its `tasks.yaml` entry, which — per Decision 3 — always wins outright over the computed default anyway.

**Confirmed, not just designed:** a Mistral-shaped config dict with no `llm.limits` block resolves to exactly `8000`, the pre-ADR-018 default, via `_resolve_default_max_chars()`'s fallback branch — checked directly, not just read from the code.

**The timeout finding from ADR 017's correction (Consequences, "Harder / needs care") remains open, as anticipated.** This ADR's mechanism only derives `max_chars` from `max_output_tokens`; it has no awareness of `providers/llm/google_aistudio.py`'s fixed `timeout=120.0`, which does not scale with request size. A large computed default (like `229,376` here) inherits the same latency/retry-budget risk ADR 017's validation surfaced at `100000` characters, unaddressed by this ADR by design (see that Consequences bullet for the reasoning) — still worth picking up as its own small, separate change if it recurs in real production use, not silently forgotten.

**A real gap found by the user during pre-merge review, before this PR landed, worth recording precisely because it almost shipped unnoticed:** the initial implementation left `cleanup`/`ortho`/`copyedit`'s static `max_chars: 100000` (ADR 017's 2026-09-11 fix) in place in `systems/s1b/tasks.yaml`, on top of adding this ADR's dynamic mechanism. Per Decision 3, an explicit `tasks.yaml` value always wins outright — so those three tasks would never have exercised the new mechanism at all, and switching `config.yaml` back to Mistral would have kept sending `100,000`-character chunks to Mistral, a size only ever validated against Gemini. Worse than a silent size mismatch: `providers/llm/mistral.py` has no `finish_reason`/truncation check at all (unlike Gemini's `finishReason=MAX_TOKENS` handling) — a truncated Mistral response at that size would have been written to disk with no error.

The twelve `s1d`/`s4`/`s5` tasks did not have this problem: their `100000` was chosen back in ADR 014, while Mistral was still the sole provider, and re-validated under Gemini in ADR 017 — real track record on both. Only `cleanup`/`ortho`/`copyedit`'s value was Gemini-only.

**Fixed before merge:** removed the explicit `max_chars` from `cleanup`/`ortho`/`copyedit` in `systems/s1b/tasks.yaml` entirely, letting them fall through to `_resolve_default_max_chars()` like any task that sets nothing. Under Google AI Studio this resolves to the same `~229,376` already validated above; under Mistral it resolves to the legacy `8000` literal — the value with years of real Mistral track record, restored automatically rather than requiring a manual `tasks.yaml` edit on every provider switch. No new API call was needed to validate this specific change: the real call already run against the small test fixture (above) explicitly used `cleanup`'s prompt with `max_chars` omitted, which is now exactly the code path the real CLI takes.

**A tradeoff accepted deliberately, not overlooked:** this makes `cleanup`/`ortho`/`copyedit`'s Google AI Studio chunk size depend on `config.yaml`'s `llm.limits` block staying populated — if that block were ever removed, these three tasks would silently fall back to `8000` again (safe, just chunk-heavy, not a correctness risk). A hardcoded `tasks.yaml` literal doesn't have that dependency. Accepted because the user identified genuine provider-switch parity as an essential requirement, which the static-literal approach could not deliver at all.
