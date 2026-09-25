# ADR 020 — Model Fallback on Sustained 503 for Google AI Studio

**Status:** Implemented (2026-09-26) — see Implementation Notes for what was and was not verified with real calls.

---

## Context

On 2026-09-25, a real `s1b ortho` run against `gemini-3.6-flash` (book `the-gobi-desert`,
manuscript chunked to 4 × ~40,000 characters after ADR 017/018's timeout finding was worked
around) failed for the wrong reason a second time. The first attempt died on repeated
`httpx.ReadTimeout`s (a size problem, addressed separately by chunking). After chunking, the
timeouts disappeared and every attempt on chunk 1 returned an immediate, fast:

```
503 from Google AI Studio: This model is currently experiencing high demand. Spikes in demand
are usually temporary. Please try again later. (UNAVAILABLE)
```

for the full 600-second retry budget (`_MAX_RETRY_SECONDS`, `providers/llm/google_aistudio.py`),
with no success. That is a *server-side capacity* condition on one specific model, not a quota
problem: the same day's AI Studio rate-limit dashboard showed 3.6 Flash at 5/20 requests per day
and 79K/250K tokens per minute — nowhere near any limit.

Two facts already established make a fix cheap:

1. **The model is not hardcoded.** `llm.model` comes from `config.yaml` and is passed straight
   into the request URL (`providers/__init__.py`, `providers/llm/google_aistudio.py`'s
   `_API_URL_TEMPLATE`). Switching models today is a one-line config edit — but a *manual* one,
   made only after a run has already burned its whole retry budget and failed.
2. **Capacity and quota are tracked per model.** ADR 017 confirmed the per-model quota buckets;
   the same dashboard showed every sibling Flash release (3.5, 3.7, 3.8, 3) at `0/20` requests
   used. A 503 on one model says nothing about its siblings.

What is missing is the automation: the retry loop retries the *same* overloaded model until its
budget is spent, when a sibling model with spare capacity is one URL away.

This also corrects, in passing, ADR 017's claim that "switching to a different Flash release will
NOT help." That was true about the daily-request *ceiling* (same 20 RPD everywhere), which is
what that passage was about. It is not true about *availability*: a sibling model is a separate
bucket and a separate capacity pool.

---

## Decision

### 1. A new optional `llm.fallback_models` list, Google AI Studio only

```yaml
llm:
  provider: google-aistudio
  model: gemini-3.6-flash
  # Optional. Tried in order when the current model returns sustained 503s
  # (see Decision 2). Omit to keep today's behavior exactly. Every entry must
  # have the same or a larger max output-token ceiling than llm.limits.max_output_tokens
  # assumes, or chunks sized for the primary can hit finishReason=MAX_TOKENS.
  fallback_models:
    - gemini-3.7-flash
    - gemini-3.5-flash
```

Unset (or empty) means no behavior change at all — this is opt-in, like `pricing:` and
`limits:`. Model names are user-maintained config, not code, for the same reason as ADR 017
Decision 7 and ADR 018 Decision 1: which siblings exist and are "similar enough" is an external,
changing fact. Which models belong in the list is the user's editorial judgement; this ADR does
not pick them.

`MistralProvider` is untouched. Cross-provider fallback is out of scope (see Alternatives).

### 2. Trigger: sustained 503 only, after a per-model window

The provider switches to the next fallback model only when **the current model has returned 503
continuously for `fallback_after_seconds`** (new optional `llm.fallback_after_seconds`, default
`60`), measured against the existing cumulative-elapsed counter. With the existing backoff
(2, 4, 8, 16, 32s) that is roughly the first five retries — long enough that a momentary blip
recovers on the same model, short enough not to spend a large share of the budget on a model
that's plainly saturated.

Deliberately **not** triggers:

- **`ReadTimeout` and other network errors.** A different model does not make a too-large
  request finish faster; that is a size problem (ADR 017/018), and switching would just repeat
  it on a fresh model.
- **429 `RESOURCE_EXHAUSTED`.** Different failure class (quota, not capacity), with real
  arguments in both directions — a sibling has its own bucket, but silently spending sibling
  quota on 429 changes cost/quota behavior in ways this ADR shouldn't decide as a side effect.
  Left as an explicit follow-up (see Alternatives).
- Non-retryable 4xx, and `200`-level `finishReason` failures (RECITATION, SAFETY, MAX_TOKENS,
  per ADR 017 Decision 3) — deterministic for the same input; a new model is a gamble, not a
  fix, and hiding it would mask a real content or sizing problem.

500/502/504 stay retry-in-place exactly as today; extending the trigger set to them is a
one-line change if real runs ever show them behaving like a saturated model.

### 3. On switch: reset backoff, keep the overall budget, stay switched

- The backoff counter resets on the new model (first retry waits 2s again), but
  `_MAX_RETRY_SECONDS` (600s) remains one **cumulative** budget across all models, so worst-case
  wall time for a fully-saturated model line is unchanged from today, not multiplied by the
  list length.
- Once switched, the provider **stays on the fallback for the remainder of that provider
  instance** — one task run — rather than probing back to the primary per chunk. This keeps a
  single manuscript's output mostly consistent instead of alternating models chunk to chunk, and
  avoids re-paying a saturated primary's 60s window on every chunk. The next task run starts
  from the configured primary again, so a recovered primary is picked up automatically at the
  next run boundary.
- If the last model in the list also hits its window, the loop simply keeps retrying that
  model until the cumulative budget is spent, then fails with the existing 503 error message,
  extended to name every model tried.
- Every switch is announced on the console, e.g.
  `503 on gemini-3.6-flash for 62s — switching to fallback gemini-3.7-flash`. Never silent.

### 4. The ledger must record what actually ran

Today `llm_text.py` records `config["llm"]["model"]` into both `manifest.yaml`'s `llm_model` and
the task ledger (via `lib/metrics.py`'s `enrich()`), which would now be wrong after a switch.
The provider gains a `models_used` attribute (ordered, deduped list of models that actually served
a request); `llm_text` and `metrics.enrich()` record that instead of the configured primary:

- No switch happened: `model: <primary>` exactly as today (ledger shape unchanged).
- Switch happened: `model` is the model that served the final request, and a new
  `models_used: [<primary>, <fallback>]` field is added to the entry, so a mixed-model
  manuscript is visible in `manifest.yaml` and `s3 dashboard`'s data, not hidden.

`usage` continues to sum across all requests, regardless of model.

### 5. Cost is approximate after a switch — accepted, not solved

`llm.pricing` is a single flat rate for one model (ADR 017 Decision 7), and stays that way. After
a switch, `cost_usd` is computed with the primary's rate against tokens that partly came from
another model. It is already labeled as *shadow* cost in `config.yaml`, not a bill, so this ADR
accepts a small, visible imprecision (the `models_used` field is the flag) over building
per-model pricing tables ahead of a real need.

### 6. Compatibility constraints, stated so they get verified rather than assumed

- **Same wire format and thinking control.** All fallback models must be Gemini 3-family
  models accepting the same `generateContent` payload including
  `thinkingConfig.thinkingLevel` (ADR 017 Decision 2). Whether 3.5 and 3.7 accept
  `thinkingLevel: "low"` identically to 3.6 is **not verified here** — it is an implementation
  checklist item, checked with one real call per candidate model.
- **Output ceiling.** Chunk sizes are derived from the primary's `llm.limits.max_output_tokens`
  (ADR 018). A fallback with a *smaller* ceiling could truncate a chunk. Documented as a
  user-maintained precondition in `config.example.yaml`; no per-model limits table is built
  now (same "speculative generality" reasoning as ADR 019's rejected preset abstraction).
- **Quality parity is not assumed.** "Similar performance" is a judgement. The implementation
  must include a small real comparison — the same short manuscript excerpt through the primary
  and each candidate fallback — before any fallback is recommended in `config.example.yaml`.

---

## Alternatives Considered

- **Keep switching models by hand (status quo).** Works, costs a full failed run's wall time
  (10+ minutes) first, and only after a human notices. This ADR exists because that's what just
  happened.
- **Cross-provider fallback (Gemini → Mistral).** Rejected for now: different wire format, API
  key, chunk-size regime (ADR 018's `8000` Mistral default vs. ~229K), retry/error semantics,
  and pricing. It is a much larger change than a model-name swap and deserves its own design.
- **Fall back on 429 as well.** Genuinely attractive (sibling models have separate quota
  buckets, so this would multiply the effective daily budget), but it changes quota/cost
  behavior rather than just availability behavior. Better as its own ADR informed by real
  usage, not folded into an availability fix.
- **Fall back on `ReadTimeout` too.** Rejected: timeouts here were a size problem, not a
  model-health problem, and a different model would hit the same wall.
- **Probe the primary again between chunks.** Rejected: alternating models within one manuscript
  hurts consistency, and re-paying the primary's failure window per chunk wastes budget. Run
  boundaries are a natural, cheap place to retry the primary.
- **Per-model pricing/limits tables now.** Deferred (Decisions 5, 6).

---

## Consequences

**Easier:**
- A saturated model no longer kills a long manuscript run; a fallback with spare capacity picks
  it up within about a minute, unattended.
- The ledger says which model actually did the work.

**Harder / needs care:**
- A manuscript can end up produced by two different models. Mitigated by stickiness (at most
  one switch point per run in the common case) and by the `models_used` field, but it is a real
  tradeoff the user accepts by populating `fallback_models`.
- `cost_usd` is approximate after a switch (Decision 5).
- The fallback list is user-maintained and can go stale as Google retires or renames models —
  same drift risk `pricing:` and `limits:` already carry; documented in the config comment.

**Explicitly not fixed by this ADR — separate problems found in the same incident:**
- **Finished chunks are not saved.** `engines/llm_text.py` writes its output file once, after
  every chunk succeeds, so a failure on chunk 3 of 4 discards chunks 1–2 and the next run starts
  over. A resume/checkpoint mechanism is a distinct design and a natural next ADR.
- **The fixed `timeout=120.0`** (ADR 017/018's open finding). Still unaddressed; `s1b ortho` /
  `copyedit` need an explicit smaller `max_chars` on this provider until it is.

---

## Implementation Checklist

- [x] Verify each candidate fallback model ID against Google's current model list — done via the
      `models` list endpoint (IDs and 65,536 output ceilings confirmed). **Not done:** confirming
      `thinkingLevel: "low"` with a real generation call — every attempt returned 503 (see notes)
- [x] Add `llm.fallback_models` / `llm.fallback_after_seconds` to `config.example.yaml` with the
      output-ceiling and drift cautions from Decisions 1 and 6; wire through `providers/__init__.py`
- [x] Extend `GoogleAIStudioProvider`: per-model 503 window, switch + backoff reset, cumulative
      budget shared across models, sticky-for-instance, `models_used`, loud console line on switch
- [x] Extend the final 503 error message to name every model tried
- [x] Update `llm_text.py` and `lib/metrics.py` to record the model(s) that actually served
      requests (`model`, plus `models_used` only when a switch occurred); confirm the ledger shape
      is byte-identical when no switch happens
- [x] Tests with a stubbed `httpx.post`: no fallback configured (unchanged); 503 then recovery on
      the same model (no switch); sustained 503 → switch → success; sustained 503 on every model
      (fails at the shared budget, error names all models); `ReadTimeout` and 429 do **not**
      trigger a switch; stickiness across multiple `complete()` calls
- [ ] One real end-to-end call forcing a switch — **deliberately skipped** by the maintainer; the
      first real production run that hits a 503 with `fallback_models` set is the validation
- [ ] Short real quality comparison — **deliberately deferred** by the maintainer; `fallback_models`
      stays commented out in `config.example.yaml`, so no fallback is recommended until it's done
- [x] Update README's `providers/llm/` description

---

## Implementation Notes (2026-09-26)

- **Stubbed tests, not real calls, carry the behavioral verification.** 14 `unittest` cases in
  `tests/test_google_aistudio_fallback.py` (stdlib only — the repo has no test framework, and
  none was added) cover every Decision 2/3/4 case listed above, plus config edge cases (duplicate
  or primary-named fallback entries) and the ledger shape with and without a switch.
- **Candidate IDs confirmed to exist:** `gemini-3.5-flash`, `gemini-3.7-flash`, `gemini-3.8-flash`
  are all listed by Google's `models` endpoint with `outputTokenLimit: 65536` (equal to the
  primary's `llm.limits.max_output_tokens`, satisfying Decision 6's ceiling precondition) and
  thinking support. Only the model *list* was used for this — it consumes no generation quota.
- **`thinkingLevel: "low"` acceptance is still unverified.** Single bounded generation requests
  to all three candidates each returned `503 UNAVAILABLE` ("high demand"). A 503 is not a 400, so
  it is weak evidence the payload shape is accepted, but it is not confirmation.
- **A finding that weakens Context's premise, honestly recorded:** at the time of the probe, *all
  three sibling models were returning the same 503 as the primary*, moments after the primary
  itself was failing. The Context section argues sibling models are separate capacity pools; the
  per-model *quota* buckets are confirmed (ADR 017), but this observation suggests *capacity*
  pressure can hit the whole Flash line at once. The fallback is still correct and harmless — it
  costs nothing when it doesn't help, and shares the same 600s budget — but it should not be
  assumed to rescue every 503. If saturation across siblings turns out to be the norm, the
  higher-value follow-ups are Flash-Lite entries (much larger RPD per the rate-limit dashboard,
  quality unevaluated) and per-chunk checkpointing, not a longer fallback list.
