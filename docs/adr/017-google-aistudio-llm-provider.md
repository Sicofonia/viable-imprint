# ADR 017 — Google AI Studio (Gemini) as the Default LLM Provider

**Status:** Implemented (with one correction made during implementation — see "The model" and Implementation Notes: this ADR now configures `gemini-3.6-flash`, not the originally-proposed `gemini-3.8-flash`). **A second, more consequential correction was made after implementation, via real account testing on 2026-09-11 — see Context's "Correction (2026-09-11)" and the second, 2026-09-11-dated Implementation Notes section: the free-tier magnitude this ADR assumed for every model except `gemini-3.8-flash` (~10 RPM / 250,000 TPM / 1,500 RPD) was wrong. The real, confirmed limits are 5 RPM / 20 RPD (peak) / 250,000 TPM (peak), flat across the entire model line — not per-model-varying as first believed.**

---

## Context

This project needs a working default LLM provider. `providers/llm/` currently holds one — Mistral — and its configured account is not usable in practice. An attempt to add Groq (ADR 015, implemented and validated, then reverted; ADR 016, never implemented) foundered not on the integration itself but on Groq's free-tier ceiling of 8,000 tokens per minute, which is smaller than a single request for twelve of this pipeline's tasks, and on Groq's Developer tier being closed to new signups indefinitely. Both of those ADRs are retained, marked `Reverted`, as a record of what was built and learned.

**Google AI Studio** (the Gemini Developer API) is the provider chosen this time, with `gemini-3.6-flash` as the model (originally `gemini-3.8-flash` — see "The model" below for why real testing changed that). The key question — the one that decided the Groq attempt — is whether its free tier can actually carry this pipeline's largest requests. It can, comfortably, for the configured model, and that makes this a far smaller change than ADR 015 turned out to be.

### The constraint that killed the last attempt does not exist here — for the model line, not for every model in it

Secondary sources put the Gemini free tier for the Gemini 3 Flash line at roughly **10 RPM / 250,000 TPM / 1,500 RPD**. Google no longer publishes a rate-limit table (its docs now say to view your active limits in AI Studio), so this needed confirming against the real account during implementation — and confirming it turned out to matter a great deal, not as a formality. See "The model" below and the Implementation Notes: the newest model in the line, `gemini-3.8-flash`, carries a **20 requests-per-day** free-tier ceiling as of this writing — evidently a launch-capacity restriction specific to that model, recently cut down from a prior 250 RPD per real user reports — while the model this ADR actually configures, `gemini-3.6-flash`, real-tested at the RPD this section originally assumed. The token-level argument below is still the right way to think about *why* Gemini is a good fit for this pipeline; it was just checked against the wrong model on the first pass.

| | Groq free tier (ADR 015) | Gemini free tier (`gemini-3.6-flash`, this ADR) |
|---|---|---|
| Tokens per minute | 8,000 | ~250,000 (~31×) |
| Largest single request this pipeline makes | ~25,000–33,000 tokens | same |
| Does that request fit? | **No — ~4× over** | **Yes — ~10–13% of the budget** |

The largest request this pipeline can make is one chunk of an `s1d brief` at the twelve expansion tasks' own `max_chars: 100000` — about 100,000 characters, so roughly 25,000–33,000 tokens depending on the tokenizer. That is a comfortable fraction of a 250,000 TPM budget. Every consequence follows from this one fact:

- **The twelve `single_chunk` tasks work unchanged.** No `max_chars` change, no request-size preflight (ADR 015's Decision 3), no batched or recursive reduce (ADR 016's whole subject). ADR 014's map-reduce machinery and its six `reduce_prompt` files are untouched and keep working exactly as they do today.
- **No follow-up ADR is implied** — for `gemini-3.6-flash`. ADR 015 shipped knowing twelve tasks were still broken; this one does not, provided the configured model is one with a usable RPD (Implementation Notes).
- **The binding limit is requests per minute, not tokens.** At the assumed ~10–15 RPM, a chunked System 1B pass over the real book in production (`historia-expedicion-asia-vol3`, 901,457 characters at the `ortho`/`copyedit` stage, 113 chunks) needs at least ~8–12 minutes of wall time, and a ~1,500 RPD budget caps the account at many such passes per day — not the binding constraint in practice. Slow, but workable, and the existing retry/backoff loop already handles the pacing. Worth knowing so an 8–12-minute run is not mistaken for a hang. **Corrected 2026-09-11, after real account testing: this conclusion is wrong.** The ~1,500 RPD figure was never actually confirmed for `gemini-3.6-flash` at volume — see the new "Correction (2026-09-11)" subsection below. The real, confirmed figure is 20 requests/day, flat across the line, the same as `3.8`'s own tested ceiling. Request count *is* the binding constraint after all, not the multi-pass-per-day comfort margin this paragraph originally described.

### Correction (2026-09-11): the assumed magnitude was wrong for every model but `3.8` — real limits are 5 RPM / 20 RPD / 250K TPM, flat across the line

Real account testing — not secondary sources, not one lucky call — now confirms Google AI Studio's actual free tier for this model line is **5 requests/minute, 20 requests/day (peak), 250,000 input tokens/minute (peak)**. This is the *same* across `gemini-3.8-flash`, `3.7`, `3.6`, and `3.5`. It is not a per-model-varying magnitude the way the section above originally assumed for everything except `3.8`.

What was actually wrong, and why it looked right at the time: the original `~10 RPM / 250,000 TPM / 1,500 RPD` figure for `gemini-3.6-flash` rested on secondary sources plus one successful call that happened to land while `3.8`'s quota was already exhausted on the same account and key. That call was real, load-bearing evidence for the *mechanism* — separate per-model quota buckets, since `3.6` succeeding while `3.8` failed proves quotas aren't pooled account-wide — but it was never sufficient evidence for the *magnitude* of `3.6`'s own bucket. One success says "this bucket has at least one request left in it"; it says nothing about whether the bucket holds 20 requests or 1,500.

So: **the mechanism claim stands, uncorrected.** Per-model quota tracking is real. What was wrong is narrower and more specific: every model's bucket turns out to be the *same* 20 RPD, not each sibling carrying its own differently-sized bucket the way `3.8`'s launch-capacity restriction implied.

The practical consequence is the opposite of what this document concluded above: at a flat 20 RPD, **request count is the binding constraint** for this pipeline, not a multi-pass-per-day comfort margin. Tracing this forward found a concrete, fixable case: `systems/s1b/tasks.yaml`'s `cleanup`, `ortho`, and `copyedit` — the three tasks that walk an entire manuscript — were still using `engines/llm_text.py`'s unoverridden `max_chars: int = 8000` default, a Mistral-era leftover never revisited when this ADR made Google AI Studio the default provider. At 8000 characters per chunk, the real production manuscript (`historia-expedicion-asia-vol3`, 901,457 characters at the `ortho`/`copyedit` stage) produces 113 chunks — 113 requests — *per task*, against a 20-request *daily* budget. Every other `llm_text` task in the repo (12 occurrences across `s1d`/`s4`/`s5`) already overrides `max_chars` to `100000`, paired with ADR 014's `single_chunk: true` guard. Raising `cleanup`/`ortho`/`copyedit` to the same, already-proven value is a pure config change — no new mechanism — and cuts each task's chunk/request count roughly 10x. See the 2026-09-11 Implementation Notes below for the fix as applied and its validation. **Superseded shortly after, within ADR 018's implementation:** a static `max_chars: 100000` in `tasks.yaml` is provider-blind — it would not revert to Mistral's historically-safe `8000` on a provider switch, unlike the twelve `s1d`/`s4`/`s5` tasks' `100000`, which has real track record on both providers. Replaced with ADR 018's provider-aware dynamic default before that mechanism's PR merged — see `docs/adr/018-provider-aware-chunk-sizing.md`'s Implementation Notes for the reasoning. This paragraph's own text is left as originally written, per this project's correction convention; the static fix it describes no longer exists in `tasks.yaml`.

Worth being honest about what this fix does and does not solve: even at roughly 10x fewer requests, `cleanup`+`ortho`+`copyedit` together against the real production manuscript can still land in the high single digits to low tens of requests — close to, or over, the 20 RPD ceiling by itself, before any other system's tasks run the same day. This is a mitigation of the binding constraint, not an elimination of it. The harder, more general version of this problem — provider-aware default sizing, so a hardcoded provider-agnostic default can't silently reintroduce this class of bug in a future task or provider/model swap — is being designed separately as ADR 018. A running "requests used today" budget-visibility mechanism is a related but deliberately separate, not-yet-built concern.

### The wire format is genuinely different from Mistral's

Mistral speaks OpenAI-style chat completions (`messages`, `choices[0].message.content`). Gemini's `generateContent` does not:

```
POST https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent
x-goog-api-key: <key>

{
  "systemInstruction": {"parts": [{"text": "<system prompt>"}]},
  "contents": [{"role": "user", "parts": [{"text": "<user text>"}]}],
  "generationConfig": {"temperature": 0.0, "thinkingConfig": {"thinkingLevel": "low"}}
}
```

Response text lives at `candidates[0].content.parts[].text`, and usage at `usageMetadata.{promptTokenCount, candidatesTokenCount, thoughtsTokenCount, totalTokenCount}`.

This is why ADR 015's approach does not transfer. That ADR extracted a shared *OpenAI-wire* base class because Mistral and Groq happened to speak the same format; Gemini does not, so there is no shared payload or response shape to factor out. What *is* still provider-agnostic is the retry/backoff/diagnostics loop — see Decision 5 for why it is nonetheless being duplicated rather than extracted this time.

### The model

**`gemini-3.6-flash`** — one generation back from the newest stable Flash release (`3.8` / `3.7` / `3.6` / `3.5`, alongside a `gemini-3-flash-preview` and a floating `gemini-flash-latest` alias), at the identical $0.75/$3.75-per-million price as `3.8`/`3.7`. This ADR originally configured `gemini-3.8-flash`; real testing during implementation found its free tier capped at 20 requests per day — evidently a launch-capacity restriction on the newest release, not representative of the line — which is far too little for this pipeline's normal chunked usage (`s1b cleanup` alone uses 2–3 requests for a single short chapter). `gemini-3.6-flash` was real-tested as an immediately-available, separately-quota'd alternative at the same price, and is what this ADR now configures. See the Implementation Notes for the evidence and the mechanism (each model has its own quota bucket, confirmed by one exhausting while the other worked on the first try).

Pinned to a specific stable release deliberately rather than using the floating `gemini-flash-latest` alias: `manifest.yaml` records `llm_model` per task as permanent provenance for published books, and `gemini-flash-latest` would record something that silently means a different model over time — a small correction to the precedent set by the previous `mistral-medium-latest` default, which had the same flaw unnoticed. The same reasoning that motivated pinning is also why this ADR does not chase the newest release reflexively: a newly-launched model can carry restrictions (like `3.8`'s current RPD) that settle down over time, and "newest" is not the same property as "best fit for this pipeline" — worth remembering if `3.8`'s free tier normalizes later and a switch back is considered.

Two model behaviours matter for the implementation:

- **Thinking is on by default and cannot be switched off.** Gemini 3 models accept `thinkingConfig.thinkingLevel` (`"low"` / `"medium"` / `"high"`; `medium` by default), with no option to disable it entirely. Thinking tokens are billed as output tokens and reported separately as `thoughtsTokenCount`.
- **Safety filtering defaults to `OFF`** for Gemini 3 models, which matters for this catalogue specifically — early-20th-century travel writing carries period-typical colonial and racial language ("land of the barbarians", "delightful old Chinamen" appear in this project's own test fixture). No `safetySettings` need to be sent; but a response can still come back blocked, and that must fail loudly rather than silently produce an empty file (Decision 3).

---

## Decision

### 1. A new, self-contained `providers/llm/google_aistudio.py`

`GoogleAIStudioProvider(LLMProvider)` implements the existing two-method interface directly — `complete(system_prompt, user_prompt, temperature)` plus the `usage` dict convention (ADR 005). No new abstraction, no change to `providers/llm/base.py`, and `providers/llm/mistral.py` is not touched at all (Decision 5).

Configured provider key: **`google-aistudio`**, matching the `GOOGLE_AISTUDIO_API_KEY` environment variable already in use. This string lands in `manifest.yaml` as per-task provenance, so it should name the product, not the model family.

**The API key goes in the `x-goog-api-key` header, not the `?key=` query parameter** the docs' curl examples use. Both work; the header is the right choice here for a concrete reason, not just hygiene: `httpx` includes the request URL in its exception messages, and this project records provider error strings into `manifest.yaml` (`_run_recorded()`'s `failed` branch, ADR 004/009). A key in the query string would therefore leak into the project's own error output and into a book's committed-adjacent manifest file on any transport failure.

### 2. Payload mapping

`system_prompt` maps to `systemInstruction` (a first-class field in this API, not a pseudo-message in a list), `user_prompt` to a single `contents` entry, and `temperature` into `generationConfig` — with `thinkingConfig.thinkingLevel` alongside it, from a new optional `llm.thinking_level` config key defaulting to `"low"`.

`low` rather than the model's own `medium` default because this pipeline's tasks are transformation and structured expansion — OCR cleanup, orthographic correction, copyediting, expanding a brief against a fixed template — not problem-solving. Thinking tokens are billed as output and cannot be eliminated, so minimising them is the only lever available. Same reasoning as the `reasoning_effort: low` choice in ADR 015, and the same shape: a config key rather than a hardcoded constant, since it is a genuine quality/cost dial, and providers that do not understand it ignore it.

### 3. Response parsing that fails loudly instead of writing plausible-looking nothing

Three distinct ways `generateContent` returns no usable text, all of which must raise a clear `ClickException` rather than hand `""` back to `engines/llm_text.py` to write to disk:

- **No candidates at all**, with `promptFeedback.blockReason` set — the *prompt* was rejected.
- **A candidate whose `finishReason` is anything other than `STOP`.** Checked as an allowlist (`STOP` is the only acceptable value), not by matching a list of bad ones — the enum is wider than the obvious cases and includes `MAX_TOKENS`, `SAFETY`, `RECITATION`, `BLOCKLIST`, `PROHIBITED_CONTENT`, `SPII`, `LANGUAGE`, and `OTHER`, so an allowlist cannot silently miss a new or unexpected value. The reason must be named verbatim in the error, because each implies a different fix — and one of them, `RECITATION` ("content too similar to training data"), is a real risk for this project specifically rather than a theoretical one: this pipeline's job is faithfully reproducing public-domain literature that is very likely in the model's training data. A `cleanup` task being refused for accurately transcribing a 1935 travel narrative is a plausible failure mode worth recognising on sight instead of debugging from scratch.
- **Empty or whitespace-only text** after extraction, for any other reason.

Text extraction concatenates the `text` of every part in `candidates[0].content.parts`, skipping any part marked `thought: true`, rather than reading `parts[0].text`. Thought parts are only returned when `includeThoughts` is set (this provider does not set it), so the filter is belt-and-braces — but the concatenation is not: a response split across multiple parts would be silently truncated by taking only the first.

#### Failing *gracefully*, not just loudly — the message has to be actionable

Raising an exception is only half the job. A blocked response is not a bug in this pipeline and not something a retry fixes, so the error has to tell the operator which of those they are looking at and what the actual next move is — the point being to recognise it on sight and go elsewhere (a different provider, or handling that passage by hand) rather than debug the pipeline.

Three properties make that work, and none of them need new machinery:

1. **It never enters the retry loop, by construction.** A blocked or truncated response arrives as **HTTP 200** with a non-`STOP` `finishReason` — a successful HTTP call carrying unusable content. The retry loop only fires on retryable statuses and transport errors, so it is bypassed naturally. Worth stating explicitly so nobody later "helpfully" adds it to the retryable set: `RECITATION` is deterministic for the same input and model, and retrying it would burn the full 600-second budget to arrive at the identical refusal.
2. **The message names the reason, what it means, and the way out.** One line per reason, rather than a generic "generation failed":

   | `finishReason` | What the message should say |
   |---|---|
   | `RECITATION` | The model refused because the output too closely reproduces its training data. Expected to recur on every retry with this model, because faithfully reproducing public-domain text is what this task is *for*. Run this task against a different provider (`pipeline.py --config <other-config>.yaml ...`, since Mistral remains configured), or handle the passage by hand. |
   | `SAFETY` / `PROHIBITED_CONTENT` / `BLOCKLIST` / `SPII` | Content filtering. Note that Gemini 3 defaults safety filtering to `OFF`, so this firing at all is a signal worth reading — see Consequences for the explicit `safetySettings` remedy. |
   | `MAX_TOKENS` | Output hit the token cap and is truncated; the fix is a smaller `max_chars` for the task or a raised output cap, not a retry. |
   | anything else | Named verbatim, with a pointer to this ADR, rather than swallowed into a generic message. |

3. **Nothing half-finished is left behind, and the reason is recorded.** `engines/llm_text.py` writes its output file (and creates the dated output directory) only after every chunk has succeeded — a deliberate property from the earlier 429 investigation — so a block partway through a 113-chunk run leaves no truncated file and no empty directory that looks like a completed-but-empty run. The failure is still *recorded*: `lib/task_loader.py`'s `_run_recorded()` writes the exception text into `manifest.yaml` as that task's `error` with status `failed`, so `s2 status` surfaces it later instead of the run vanishing from the record. And because `llm_text` echoes `<task> chunk i/total...` immediately before each call, the last line printed identifies exactly which chunk was refused — useful for finding the offending passage in a long manuscript, and the reason the provider itself does not need chunk-index awareness.

### 4. Usage mapping that keeps `s3 dashboard` correct

The project's `usage` contract is `{prompt_tokens, completion_tokens, total_tokens}` (ADR 005), consumed by `lib/metrics.py` and `s3 dashboard`. Gemini's fields map as:

| Project field | Gemini field(s) |
|---|---|
| `prompt_tokens` | `usageMetadata.promptTokenCount` |
| `completion_tokens` | `usageMetadata.candidatesTokenCount` **+** `usageMetadata.thoughtsTokenCount` |
| `total_tokens` | `usageMetadata.totalTokenCount` |

The addition in the middle row is the load-bearing detail. Thinking tokens are billed at the output rate, so mapping only `candidatesTokenCount` into `completion_tokens` would silently under-report every task's real cost — and with thinking on by default and undisableable, that under-reporting would apply to every single call. With this mapping, `lib/metrics.py`, `lib/dashboard.py`, and `s3 dashboard` need no changes whatsoever.

### 5. The retry loop is duplicated, deliberately, and `mistral.py` is not touched

The retry/backoff/diagnostics loop in `mistral.py` (retryable statuses, the 600-second time budget, 2s→60s backoff, `httpx.TransportError` handling, `retry-after` respect, real-error-detail extraction) is transport-level and would work for Gemini unchanged. It is nonetheless being **copied into the new provider rather than extracted into a shared helper**, on the explicit instruction to keep the new provider self-contained and leave the working Mistral path completely alone.

This is a real tradeoff, named rather than glossed: two copies of the subtlest code in the repository — code that absorbed two separate production incidents' worth of hardening — means the next fix to it has to be applied twice or silently misses one. Two mitigations, both cheap: a comment at the top of each copy pointing at the other, and an explicit note that **a third provider is the trigger to extract it** (as a plain function taking url/headers/payload/display-name, not the class hierarchy ADR 015 built — Gemini's differing wire format shows that a shared *chat-format* base was the wrong seam anyway; the shared thing is the HTTP retry, nothing more).

Gemini-specific pieces that do differ from Mistral's copy: a rate-limit hint naming the free tier's dimensions and pointing at AI Studio, and error-detail extraction reading Google's `{"error": {"message", "status", "code"}}` shape. A `429` here is `RESOURCE_EXHAUSTED`; at 10 RPM it will be hit routinely on long chunked runs, so the backoff doing visible work mid-run is expected behaviour, not a fault.

### 6. `generateContent` now, Interactions API later — recorded as a live decision, not a closed one

Google's docs label `generateContent` "Legacy" in their navigation and state that the newer **Interactions API** (`v1beta2/interactions`) is recommended "for all new development", while `generateContent` "remains fully supported".

This ADR deliberately builds on `generateContent`, because every advantage the Interactions API offers is something this pipeline specifically does not use: server-side conversation history (every call here is stateless and single-turn), observable multi-step execution, native tool use, agentic workflows, long-running background tasks. Against that, it is a newer surface that had breaking changes as recently as May 2026, and its steps-based response timeline is a worse fit for a `complete()` method that wants one string and a token count. `generateContent`'s stable, widely-documented `candidates`/`usageMetadata` shape maps onto this project's existing interface almost exactly.

**This is expected to need revisiting, and these are the triggers** — recorded here so the decision is inherited rather than rediscovered:

- Google attaches an actual deprecation date or sunset notice to `generateContent` (currently there is none — "Legacy" in a docs nav is not a deprecation).
- This project ever genuinely needs tool use, multi-turn state, or agentic multi-step behaviour from an LLM task. Nothing in Systems 1B/1D/3/4/5 needs it today; a future System 1A acquisition-research task might.
- The Interactions API gains something material that `generateContent` lacks — cheaper pricing on the same model, a capability a task needs, or a rate-limit advantage.

Because the provider is self-contained (Decision 5) and hidden behind `LLMProvider.complete()`, switching surfaces later means rewriting one file's internals, with no caller changes anywhere — the migration cost is bounded by design, which is part of why building on the current surface now is defensible.

### 7. Config, and `llm.model` becomes required

```yaml
llm:
  provider: google-aistudio
  model: gemini-3.6-flash
  temperature: 0.0
  thinking_level: low
  pricing:
    prompt_per_million: 0.75
    completion_per_million: 3.75
```

`GOOGLE_AISTUDIO_API_KEY` joins `.env.example`; `get_llm_provider()` gains a `google-aistudio` branch and its "Unknown LLM provider" message lists both providers.

Two details worth stating rather than leaving in the config comments alone:

- **`llm.model` becomes required.** The two hardcoded `"mistral-medium-latest"` fallbacks (`providers/__init__.py`, `engines/llm_text.py`) are wrong the moment a second provider exists — a Mistral model name sent to Gemini simply fails. A missing `llm.model` should raise a clear error naming the config key instead. (This was also ADR 015's Decision 2; it was correct then and is correct now, and it came back with the revert.)
- **Pricing is recorded at the real paid rates even though the free tier costs nothing**, as a *shadow* cost — what a run would have cost if billed. A cost column reading `$0.00` for the dependency the whole pipeline runs on cannot warn about anything, which is the point of ADR 005's cost tracking. The tradeoff is that ADR 011's reported margin subtracts a cost never actually paid; that margin is already labelled as tracked-API-cost-only, so this widens a flagged approximation rather than introducing a new one. **These figures are promotional and double on 1 January 2027** ($1.50 / $7.50) — a dated, known change worth a config comment so the dashboard does not quietly halve its cost estimates from that day on.

**Temperature stays at `0.0` and is flagged, not silently changed.** That is this project's fidelity-first default for `cleanup`/`ortho`/`copyedit`, with per-task overrides for the marketing tasks. Whether `0.0` suits Gemini 3 is an open question — the previous provider's docs recommended a higher floor for its model and ADR 015 chose not to change two variables at once for exactly this reason. Same call here: start at `0.0`, evaluate on the fidelity-critical tasks, adjust as a separate deliberate step (Implementation Checklist).

---

## Alternatives Considered

- **The Interactions API instead of `generateContent`.** Covered in full in Decision 6, including the triggers for switching. Short version: Google recommends it for new work, but its advantages are all features this pipeline does not use, it changed incompatibly as recently as May 2026, and its steps-based response is a worse fit for a single-turn `complete()`.
- **Google's OpenAI-compatibility endpoint** (`/v1beta/openai/chat/completions`). Would have let a Gemini provider reuse Mistral's exact payload and response handling, and would have made ADR 015's reverted `openai_chat.py` base genuinely applicable. Rejected: it is a compatibility shim over the native API, so it can lag native features and behaviours, and it obscures precisely the fields this integration needs to handle correctly — `thoughtsTokenCount` for accurate cost (Decision 4), `finishReason`/`promptFeedback` for blocked-response detection (Decision 3), `thinkingConfig` for cost control (Decision 2). Trading those away to save a payload-shape mapping is the wrong trade.
- **Extracting the retry loop into a shared helper now.** The technically cleaner option and what ADR 015 did (for a differently-drawn seam). Not done here on explicit instruction to keep the new provider self-contained and the working Mistral path untouched, with a third provider named as the trigger to revisit (Decision 5).
- **The `google-genai` Python SDK instead of raw `httpx`.** Rejected for the same reason `mistral.py` never used an SDK: this project's retry and diagnostic layer operates on HTTP status codes, response headers, and `httpx.TransportError`. An SDK wraps all three in its own exception taxonomy, so the hardening would have to be rewritten against that taxonomy for no gain, plus a new dependency. The precedent has held up across two providers now.
- **A floating `gemini-flash-latest` model alias.** Matches the previous `mistral-medium-latest` precedent and always tracks the newest Flash. Rejected: it records meaningless provenance in `manifest.yaml` for a published book, and a silent model change is a genuine risk to prompt reliability that this project has repeatedly had to hand-tune (ADR 002, 006, 014, and ADR 015's own Groq-specific output defect).
- **`gemini-3-flash-preview`.** The literal reading of "Gemini 3 Flash" and the cheapest option at $0.50/$3.00. Rejected in favour of a stable release: a preview model can change or be withdrawn without the stability commitment the numbered stable line carries, which is a poor foundation for a pipeline whose prompt reliability is tuned per model.
- **`gemini-3.8-flash`, the newest stable release** — this ADR's own original choice, superseded during implementation. Real testing found its free tier capped at 20 requests per day (see "The model" and Implementation Notes), evidently a launch-capacity restriction rather than a property of the model line. Worth naming explicitly as a lesson, not just a footnote: "newest stable release" was treated as a reasonable tie-breaker over `3.6`/`3.7` in the original design, and in this case it was the wrong one — a model's free-tier quota can differ sharply from its siblings' right after launch, in a way no amount of reading documentation would have caught without a real call.
- **Pre-emptively re-adding ADR 015's Groq-era fixes** (the request-size preflight, the bracketed-Roman-numeral output normalizer). Rejected deliberately. The preflight solved a ceiling that does not exist here (see Context), and the normalizer fixed a defect observed in *one specific model's* output; carrying a model-specific workaround forward to a different model on the assumption it will misbehave the same way is exactly backwards. Both remain documented in ADR 015's retained record if Gemini turns out to need something similar — which real testing, not assumption, should decide.

---

## Consequences

**Easier:**

- The pipeline has a working default provider across every system (1B, 1D, 3, 4, 5) again, with all twelve large `single_chunk` tasks working on the free tier — the thing the Groq attempt never achieved.
- Far smaller blast radius than ADR 015: one new file, one new branch in `get_llm_provider()`, a one-line fallback removal, plus config and docs. No engine logic, no chunking changes, no prompt changes, no changes to `lib/metrics.py`, `lib/dashboard.py`, or any `tasks.yaml`.
- The README's "swapping providers is one file and one line of config" claim gets a second, genuinely different demonstration — this time across two *incompatible* wire formats, which is a stronger test of the abstraction than two OpenAI-compatible providers were.
- Free at current volumes, with an honest shadow cost recorded for when that changes.

**Harder / needs care:**

- **Prompt reliability does not transfer with the provider.** Every prompt in `prompts/` is Spanish and was tuned against Mistral over many real-call iterations; `engines/llm_text.py`'s three output normalizers were each added for a specific *Mistral* defect. ADR 015's retained record is direct evidence this matters: Groq's very first real `ortho` call produced a defect (bracket-wrapped Roman numerals) that no existing normalizer caught. Expect a comparable round for Gemini, and check the fidelity-critical Spanish tasks first.
- **Two copies of the retry/backoff/diagnostics loop** (Decision 5, chosen deliberately) — the next fix to it must be applied to both, or it silently reaches only one.
- **`RECITATION` and `SAFETY` finish reasons are a live risk, not a formality**, given a corpus of public-domain text likely present in training data and full of period-typical colonial language. Decision 3 makes them legible and actionable — named reason, plain-language meaning, the concrete way out, recorded in `manifest.yaml`, no half-written output — but it cannot make them impossible. If safety blocking is observed, explicit `safetySettings` with `OFF` thresholds is the documented remedy (filtering already defaults to `OFF` on Gemini 3, so that would be belt-and-braces). **There is no equivalent override for `RECITATION`:** if it fires on a task whose whole purpose is faithful transcription, the realistic options are running that task on a different provider or handling the passage by hand — which is exactly why the error message says so outright rather than leaving the operator to infer it. A `RECITATION` block is therefore the one failure mode in this integration that no amount of code can fix, only route around; treating it as a provider-selection signal rather than a bug is the intended response.
- **~10 RPM makes long chunked runs slow** — roughly 8–12 minutes minimum for a 113-chunk manuscript pass. Expected, not a malfunction, but worth knowing before assuming a run has hung.
- **Thinking cannot be disabled**, so every call spends some output-token budget on it. `thinkingLevel: "low"` minimises but does not remove this, and it inflates `completion_tokens` (correctly — it is billed that way).
- **`llm.model` becomes required**, a breaking config change for any checkout relying on the old fallback. Intended, and belongs in the README's setup section.
- **Building on a docs-labelled "Legacy" API surface**, with the migration path and its triggers recorded in Decision 6 rather than left implicit.
- **Free-tier quotas are per-model and can vary sharply between siblings in the same line — confirmed the hard way, not a hypothetical.** `gemini-3.8-flash`, this ADR's original choice, tested at a 20-requests-per-day free-tier cap during implementation (a real 429 body: `"Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.8-flash"`) — low enough that even `s1b cleanup` alone against the test fixture couldn't complete. `gemini-3.6-flash` (this ADR's configured model) worked immediately against a fresh call while `3.8`'s quota was still exhausted, confirming quotas are tracked independently per model rather than per account. The practical lesson, worth keeping for any future model choice on this provider: **prefer a stable, already-settled model over the newest release, and confirm its real RPD with a live call before committing to it in an ADR** — reading documentation would not have caught this; only a real request did. Also means the "10 RPM / 250K TPM / 1,500 RPD" figures in this document are specific to `gemini-3.6-flash` as tested, not a property of "Gemini 3 Flash" as a category, and should be re-confirmed if the configured model ever changes. **Confirmed sooner than this bullet expected, and one layer deeper: re-confirmed 2026-09-11 via real account testing.** It wasn't only `gemini-3.8-flash` carrying an anomalous quota — `gemini-3.6-flash`'s own assumed figure was also wrong, by roughly 75x (1,500 assumed vs. 20 real), and the same flat 20 RPD applies to every model in the line. See Context's "Correction (2026-09-11)" subsection and the 2026-09-11-dated Implementation Notes below. This is the exact "confirm against a real account, not documentation or secondary sources" lesson this bullet already drew — it just turned out to apply one layer deeper than the original test happened to reach.
- **A real, separate friction observed during implementation, worth distinguishing from the RPD finding above:** `gemini-3.8-flash` also returned genuine `503 UNAVAILABLE` ("This model is currently experiencing high demand") on some real calls, correctly retried and eventually recovered. This is ordinary launch-period capacity pressure, unrelated to the per-model RPD issue, and not something to read as a sign this integration is unreliable — the retry loop handled both kinds of friction correctly, which is itself part of what the real testing confirmed.

---

## Implementation Checklist

**Provider:**

- [x] Create `providers/llm/google_aistudio.py`: `GoogleAIStudioProvider(LLMProvider)` with the `generateContent` endpoint, `x-goog-api-key` header auth, the Decision 2 payload mapping, the Decision 3 response parsing (no-candidates / non-`STOP` `finishReason` / empty-text, all raising clear errors; multi-part text concatenation skipping `thought` parts), the Decision 4 usage mapping, and its own copy of the retry/backoff loop with a Gemini-specific rate-limit hint and Google's error-body shape — plus the cross-reference comment to `mistral.py`'s copy
- [x] Write the per-`finishReason` guidance messages per Decision 3's table, and confirm a blocked response is *not* routed into the retry loop (it arrives as HTTP 200, so this should hold by construction — verify rather than assume)
- [x] Add the `google-aistudio` branch to `get_llm_provider()` with a `GOOGLE_AISTUDIO_API_KEY` check mirroring the Mistral one; update the "Unknown LLM provider" message to list both
- [x] Remove both `"mistral-medium-latest"` fallbacks (`providers/__init__.py`, `engines/llm_text.py`); raise a clear `ClickException` naming `llm.model` when absent
- [x] Confirm `providers/llm/mistral.py` and `providers/llm/base.py` are untouched, and that no engine, `lib/`, or `tasks.yaml` file needed changing

**Config and docs:**

- [x] Add `GOOGLE_AISTUDIO_API_KEY` to `.env.example`
- [x] Update `config.yaml` and `config.example.yaml` per Decision 7, including comments on the shadow-cost reasoning and the 1 January 2027 price change
- [x] Update the README: setup section (new key, `llm.model` now required), the provider-abstraction paragraph (two providers, two different wire formats, no shared base), and the requirements list
- [x] Confirm the account's real rate limits — done, and materially corrected the model choice as a result; see Implementation Notes. **Re-confirmed 2026-09-11 via further real account testing: the assumed magnitude was also wrong for `gemini-3.6-flash` specifically, not just `3.8`. See Context's "Correction (2026-09-11)" and the second, 2026-09-11-dated Implementation Notes section.**

**Validation (real calls, not simulated) — `books/test` only, and never `s1b translate`:**

Scoped exactly as ADR 015's validation was, and for the same reason: `translate` calls DeepL, not the LLM provider, and re-running it spends real DeepL character quota reserved for production books. `books/test/s1b/translated/es/zayagan-chp1.txt` already exists from a prior real translation and is reused as-is.

- [x] `s1b cleanup` on `books/test/s1b/source/zayagan-chp1.txt` — first real Gemini call. English source, pre-translation, so this checks OCR-cleanup fidelity and whether the three existing normalizers still suffice or new artifacts appear
- [x] `s1b ortho` and `s1b copyedit` against the **existing** translated fixture — the two Spanish, fidelity-critical tasks, and where `temperature: 0.0` against a model whose default is higher is most likely to show
- [x] One large `single_chunk` task against a real oversized brief (`historia-expedicion-asia-vol3`'s 460,733-character brief, which chunks to 5 pieces) — the case Groq could never run; confirm both the per-chunk map calls and the reduce call succeed, and that no request-size guard is needed
- [x] One small non-book task (`s5 evaluate` against an existing candidate brief) — confirms the non-book roots work unchanged
- [x] Confirm `s3 dashboard` reports Gemini usage and shadow cost correctly, and specifically that `thoughtsTokenCount` is included in `completion_tokens` (compare a run's reported completion tokens against `usageMetadata` from a raw call, so the addition in Decision 4 is verified rather than assumed)
- [x] Verify the blocked-response path without waiting for it to happen naturally: feed the provider a synthetic `generateContent` response body with `finishReason: "RECITATION"` (and one with no candidates plus a `promptFeedback.blockReason`) and confirm each raises immediately, names the reason, and gives the provider-switch guidance — a real `RECITATION` cannot be reliably forced on demand, so this is the one part of the validation that is better checked against a constructed response than a live call
- [x] Watch for and record any `finishReason` other than `STOP` during the real calls above — especially `RECITATION` on the faithful-transcription tasks — and document what was seen either way, including "never fired" as a result worth recording

---

## Implementation Notes (2026-09-10)

Built and tested end-to-end against real invocations, not simulated. One material correction was made mid-implementation, found by a real call rather than assumed — documented here in full rather than silently folded into a clean final state.

**Synthetic verification, before any real API call.** Per the checklist, the blocked-response path was verified against constructed `generateContent` response bodies (mocked `httpx.post`) before spending anything real: a `finishReason: "RECITATION"` candidate raised immediately, named the reason, and gave the provider-switch guidance; a no-candidates response with `promptFeedback.blockReason` set raised with that reason; an empty-text response despite `finishReason: "STOP"` raised; a multi-part response with a `thought: true` part correctly concatenated only the non-thought parts. Separately confirmed that a genuine `429`/`503` *does* enter the retry loop and recovers correctly (via a scripted two-call sequence: fail once, succeed on retry), and — the specific thing Decision 3 depends on — that a blocked/truncated response makes exactly **one** HTTP call with **zero** retries or sleeps, confirmed by call-counting and mocking `time.sleep`, not just read from the code.

**A real, material correction: `gemini-3.8-flash`'s free tier turned out to be 20 requests per day, not ~1,500.** Discovered on the first real `s1b cleanup` call, which needed 16.7 minutes and absorbed both real `503 UNAVAILABLE` ("This model is currently experiencing high demand") and a real `httpx.ReadTimeout` before succeeding — both correctly retried by the existing loop. Every subsequent real call that day, including a *solo, unhurried* retry with nothing else running, kept failing with the same `429`:

```
* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.8-flash
```

A web search (not just the account dashboard, which wasn't accessible from this session) turned up a live Google AI Developers Forum thread titled exactly *"Gemini 3.8 Flash Free Tier 20 RPD Is Too Limited for Practical Evaluation"*, confirming this is real, current, and reportedly a recent cut down from a prior 250 RPD — evidently a launch-capacity restriction on the newest model specifically, not a property of the Gemini 3 Flash line. The same search found secondary reports that `gemini-3.6-flash`/`gemini-3.5-flash` share the ~1,500 RPD / 15 RPM figures this ADR originally assumed for "Gemini 3 Flash" generally.

**Confirmed directly, not just from secondary sources, before committing to the fix:** one minimal real call against `gemini-3.6-flash` succeeded immediately while `gemini-3.8-flash`'s quota was still fully exhausted from the same account and the same API key — proving free-tier quotas are tracked **per model**, not per account, and that switching models is a real, immediate fix rather than a hopeful guess. `providers/llm/google_aistudio.py` needed zero code changes for this — the model is a config value, never hardcoded — so the fix was entirely: `config.yaml`/`config.example.yaml`'s `model:` value, this ADR's text, and one example string in `providers/__init__.py`'s error message.

**Also found, and worth separating from the RPD issue above so neither gets misdiagnosed as the other:** running three validation tasks *concurrently* (against `books/test`, the real production book, and `candidates/`, in parallel background processes) caused all three to genuinely starve each other against the shared per-minute request budget, and two of them exhausted their own 600-second retry budgets and failed cleanly — the give-up path worked exactly as designed (clean error, correctly recorded as `failed` in each book's `manifest.yaml`, rate-limit hint appended), it just couldn't win a race this project's pipeline was never designed to run (ADR 001: "dumb and sequential... no orchestration layer"). Concurrent execution during validation was this session's own choice, not a normal usage pattern this integration needs to support — re-run sequentially afterward, cleanly, with zero retries needed on any of them.

**Once `gemini-3.6-flash` was configured, every validation item passed cleanly, quickly, with zero retries:**

- **`s1b cleanup`: clean, 81s.** OCR page-break artifacts correctly removed, foreign term `kang` correctly wrapped in `[i]...[/i]` per the prompt's own rule. No defects.
- **`s1b ortho`: clean, 43s.** All three orthographic rules correctly applied: guillemets (`«...»`) for quoted text, Roman numerals wrapped in brackets (`CAPÍTULO [I]`, `el siglo [VI]`), foreign terms in `[i]...[/i]` (`kang`, `sarais`, `bodhisattvas`).
- **A real correction to this project's own prior understanding, surfaced by this exact output:** the bracketed Roman numerals above are **not** a model defect. `prompts/s1b/ortho_task.txt` (this user's own personal prompt) Rule 2 explicitly instructs exactly this: *"Los números romanos deben ir en versalitas, pero prefiero que me los marques entre corchetes"* ("Roman numerals should be in small caps, but I prefer you mark them in brackets"). The reverted ADR 015/Groq work had misdiagnosed this identical, correct output as a defect and shipped a normalizer (`_strip_bracketed_roman_numerals()`) that stripped brackets the user's own prompt explicitly asked for — a real mistake, caught here only because that code no longer exists after the revert, so there was nothing to carry forward incorrectly. No normalizer for this is added in this implementation. Worth remembering if this ever comes up again with a future provider: check the prompt's own rules before diagnosing repeatable "bracket" output as a model quirk.
- **`s1b copyedit`: clean, 31s.** Per this validation's scoping (run against the raw translated fixture directly, not chained after `ortho`'s output — same as the ADR 015 validation before it), its output correctly has no brackets: Rule 11 preserves brackets already present in its input, and this run's input had none. Not a defect; a consequence of the deliberate validation shortcut, not of real production chaining (where `copyedit` normally receives `ortho`'s bracketed output and Rule 11 would apply for real).
- **`s1d synopsis` against the real 460,733-character brief: clean, 87s, zero retries.** All 5 map-step chunks and the reduce call succeeded on the first attempt — the exact case Groq's free tier could never handle at all. Output is one coherent two-paragraph synopsis spanning the whole three-year expedition, no `<borrador>` tag leakage, no duplication.
- **`s5 evaluate`: clean, 9s.** Confirms the non-book (`candidates/`) root path works unchanged.
- **`s3 dashboard`: confirmed correct**, both per-book and portfolio-wide. `google-aistudio`/`gemini-3.6-flash` rows show real token usage and shadow cost; `completion_tokens` visibly includes thinking tokens (e.g. `cleanup`: 4,306 prompt vs. 7,986 completion — thinking tokens roughly double the naive output-only figure, confirming Decision 4's mapping is both correct and load-bearing, not a rounding nicety); a pre-existing `mistral`/`mistral-medium-latest` row for the same book correctly shows `(stale)`, cascade-invalidated by ADR 009 when `cleanup` was re-run under the new provider — the same mixed-provenance consequence ADR 015 first documented, observed again for real.
- **No `finishReason` other than `STOP` fired on any real call in this validation pass** — `RECITATION` never triggered, including on the faithful-transcription tasks (`cleanup`, `ortho`, `copyedit`) this ADR flagged as the specific risk. Recorded honestly as "not observed," not "impossible" — Consequences' assessment of `RECITATION` as a live risk stands; this pass simply didn't hit it.

**Not exercised for real in this pass:** `gemini-3.8-flash`'s actual output quality and prompt-reliability behavior were never meaningfully tested, since every real call against it failed before producing usable text. Nothing is known about whether it would have exhibited its own model-specific output quirks (the way Groq did) — moot for now since it isn't the configured model, but worth knowing if `3.8`'s RPD situation changes and a switch back is considered later.

---

## Implementation Notes (2026-09-11)

The corrected free-tier figures above (5 RPM / 20 RPD / 250K TPM, flat across the line) came from real account testing done directly by the user against the live AI Studio dashboard, not from a call made in this session. The fix this correction implied — raising `systems/s1b/tasks.yaml`'s `cleanup`/`ortho`/`copyedit` from the unoverridden `max_chars: 8000` default to `100000` — was applied and then validated for real, since it borrows a chunk-size precedent (`100000`) that was only ever proven against compression-shaped tasks (`s1d`/`s4`/`s5`), not the roughly 1:1 input:output transformation shape `cleanup`/`ortho`/`copyedit` actually have. **This static `100000` no longer exists in `tasks.yaml` as of ADR 018's implementation** — the real call and validation narrated below still stand as evidence the value itself is safe, but `cleanup`/`ortho`/`copyedit` now reach a comparable, provider-computed value dynamically rather than through a hardcoded literal, specifically so a future provider switch reverts to Mistral's own historically-safe default instead of carrying Gemini's value along unconditionally. See `docs/adr/018-provider-aware-chunk-sizing.md`.

**Validation method, chosen to spend as little of the day's 20-request budget as possible:** rather than running `cleanup` against the full production manuscript (which would consume one request per chunk, ~9 requests at the new `100000` setting), a single real chunk was extracted using the actual `lib/chunker.chunk_by_paragraphs()` logic against the real production source (`books/historia-expedicion-asia-vol3/s1b/source/source.txt`) — the genuine first 96,882-character chunk `cleanup` would produce at `max_chars: 100000` — and run once, in isolation, inside a throwaway scratch book (`books/chunk-validation-scratch/`, not committed, deleted after use) so the real book's own ledger stayed untouched.

**Result: clean success, no `MAX_TOKENS`, confirming `100000` is safe for these three tasks — but not fast.** The real call:

```
uv run python pipeline.py s1b cleanup books/chunk-validation-scratch/s1b/source/excerpt.txt
```

completed in **359.19 seconds** (~6 minutes) — far longer than a normal-sized `cleanup` call (81s in the original 2026-09-10 validation above) — after two `httpx.ReadTimeout`s that the existing retry loop correctly absorbed and recovered from. `status: done` in the resulting `manifest.yaml` entry confirms `finishReason` was `STOP`, not `MAX_TOKENS`; the output file was read directly and confirmed non-truncated, ending on a complete sentence. Real usage: `prompt_tokens: 23124`, `completion_tokens: 22660` (includes thinking, per Decision 4's mapping), `total_tokens: 45784`. Output length (97,446 characters) came out almost exactly 1:1 against the 96,882-character input, confirming the input:output ratio these three tasks were flagged as having — and landing comfortably under any plausible per-response output-token ceiling, with real margin to spare.

**A distinct, real finding, worth separating from the `MAX_TOKENS` question this validation was primarily checking:** `providers/llm/google_aistudio.py`'s `complete()` sends every request with a **fixed** `timeout=120.0`, regardless of request or expected-response size. For a chunk this large (~25,000 prompt tokens, generating a comparably large ~1:1 output plus mandatory thinking overhead), the response routinely takes longer than 120 seconds to arrive, which is why the retry loop fired twice before the call that actually completed. The retry loop's existing behavior (2s→4s backoff, capped at 60s, 600s cumulative budget) absorbed this correctly and the call ultimately succeeded — but each retried attempt very plausibly still reached Google's servers and was processed as a real request before the client gave up waiting, meaning a structurally-slow call like this one can cost more than one of the day's 20 requests for what registers as a single successful task run. This is a real, separate risk from the RPD magnitude correction above — a fixed, size-unaware client timeout — and is named here rather than silently accepted: worth considering, if this recurs, either as a follow-up to `providers/llm/google_aistudio.py`'s `timeout=120.0` constant directly, or as an angle for ADR 018 to account for (a large `max_chars` value avoids `MAX_TOKENS` but can trade it for a timeout/retry-budget risk instead) when that ADR's mechanism is eventually implemented.

**Scratch book (`books/chunk-validation-scratch/`) deleted after this validation, per this project's established scratch-fixture pattern (ADR 011) — not committed.**
