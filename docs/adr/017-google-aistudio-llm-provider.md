# ADR 017 — Google AI Studio (Gemini) as the Default LLM Provider

**Status:** Proposed.

---

## Context

This project needs a working default LLM provider. `providers/llm/` currently holds one — Mistral — and its configured account is not usable in practice. An attempt to add Groq (ADR 015, implemented and validated, then reverted; ADR 016, never implemented) foundered not on the integration itself but on Groq's free-tier ceiling of 8,000 tokens per minute, which is smaller than a single request for twelve of this pipeline's tasks, and on Groq's Developer tier being closed to new signups indefinitely. Both of those ADRs are retained, marked `Reverted`, as a record of what was built and learned.

**Google AI Studio** (the Gemini Developer API) is the provider chosen this time, with `gemini-3.8-flash` as the model. The key question — the one that decided the Groq attempt — is whether its free tier can actually carry this pipeline's largest requests. It can, comfortably, and that makes this a far smaller change than ADR 015 turned out to be.

### The constraint that killed the last attempt does not exist here

Secondary sources put the Gemini free tier for the Gemini 3 Flash line at roughly **10 RPM / 250,000 TPM / 1,500 RPD**. Google no longer publishes a rate-limit table (its docs now say to view your active limits in AI Studio), so these figures need confirming against the real account during implementation — but the margin is wide enough that the conclusion holds even if they are somewhat off:

| | Groq free tier (ADR 015) | Gemini free tier (this ADR) |
|---|---|---|
| Tokens per minute | 8,000 | ~250,000 (~31×) |
| Largest single request this pipeline makes | ~25,000–33,000 tokens | same |
| Does that request fit? | **No — ~4× over** | **Yes — ~10–13% of the budget** |

The largest request this pipeline can make is one chunk of an `s1d brief` at the twelve expansion tasks' own `max_chars: 100000` — about 100,000 characters, so roughly 25,000–33,000 tokens depending on the tokenizer. That is a comfortable fraction of a 250,000 TPM budget. Every consequence follows from this one fact:

- **The twelve `single_chunk` tasks work unchanged.** No `max_chars` change, no request-size preflight (ADR 015's Decision 3), no batched or recursive reduce (ADR 016's whole subject). ADR 014's map-reduce machinery and its six `reduce_prompt` files are untouched and keep working exactly as they do today.
- **No follow-up ADR is implied.** ADR 015 shipped knowing twelve tasks were still broken; this one does not.
- **The binding limit is requests per minute, not tokens.** At 10 RPM, a chunked System 1B pass over the real book in production (`historia-expedicion-asia-vol3`, 901,457 characters at the `ortho`/`copyedit` stage, 113 chunks) needs at least ~12 minutes of wall time, and 1,500 RPD caps the account at roughly 13 such passes per day. Slow, but workable, and the existing retry/backoff loop already handles the pacing. Worth knowing so a 12-minute run is not mistaken for a hang.

### The wire format is genuinely different from Mistral's

Mistral speaks OpenAI-style chat completions (`messages`, `choices[0].message.content`). Gemini's `generateContent` does not:

```
POST https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent
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

`gemini-3.8-flash` — the newest stable model in the Flash line (`3.8` / `3.7` / `3.6` / `3.5`, alongside a `gemini-3-flash-preview` and a floating `gemini-flash-latest` alias). Pinned deliberately rather than using the floating alias: `manifest.yaml` records `llm_model` per task as permanent provenance for published books, and `gemini-flash-latest` would record something that silently means a different model over time. This is a small correction to the precedent set by the previous `mistral-medium-latest` default, which had the same flaw unnoticed.

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
  model: gemini-3.8-flash
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
- **`RECITATION` and `SAFETY` finish reasons are a live risk, not a formality**, given a corpus of public-domain text likely present in training data and full of period-typical colonial language. Decision 3 makes them legible; it does not make them impossible. If blocking is observed, explicit `safetySettings` with `OFF` thresholds is the documented remedy for the safety case (filtering already defaults to `OFF` on Gemini 3, so this would be belt-and-braces) — there is no equivalent override for `RECITATION`, which would need a per-task workaround if it ever fires.
- **10 RPM makes long chunked runs slow** — roughly 12 minutes minimum for a 113-chunk manuscript pass, and about 13 such passes per day against the 1,500 RPD cap. Expected, not a malfunction, but worth knowing before assuming a run has hung.
- **Thinking cannot be disabled**, so every call spends some output-token budget on it. `thinkingLevel: "low"` minimises but does not remove this, and it inflates `completion_tokens` (correctly — it is billed that way).
- **`llm.model` becomes required**, a breaking config change for any checkout relying on the old fallback. Intended, and belongs in the README's setup section.
- **Building on a docs-labelled "Legacy" API surface**, with the migration path and its triggers recorded in Decision 6 rather than left implicit.
- **The free-tier figures are secondary-sourced.** Google no longer publishes the table; they must be confirmed in AI Studio, and this ADR's arithmetic corrected if they differ materially.

---

## Implementation Checklist

**Provider:**

- [ ] Create `providers/llm/google_aistudio.py`: `GoogleAIStudioProvider(LLMProvider)` with the `generateContent` endpoint, `x-goog-api-key` header auth, the Decision 2 payload mapping, the Decision 3 response parsing (no-candidates / non-`STOP` `finishReason` / empty-text, all raising clear errors; multi-part text concatenation skipping `thought` parts), the Decision 4 usage mapping, and its own copy of the retry/backoff loop with a Gemini-specific rate-limit hint and Google's error-body shape — plus the cross-reference comment to `mistral.py`'s copy
- [ ] Add the `google-aistudio` branch to `get_llm_provider()` with a `GOOGLE_AISTUDIO_API_KEY` check mirroring the Mistral one; update the "Unknown LLM provider" message to list both
- [ ] Remove both `"mistral-medium-latest"` fallbacks (`providers/__init__.py`, `engines/llm_text.py`); raise a clear `ClickException` naming `llm.model` when absent
- [ ] Confirm `providers/llm/mistral.py` and `providers/llm/base.py` are untouched, and that no engine, `lib/`, or `tasks.yaml` file needed changing

**Config and docs:**

- [ ] Add `GOOGLE_AISTUDIO_API_KEY` to `.env.example`
- [ ] Update `config.yaml` and `config.example.yaml` per Decision 7, including comments on the shadow-cost reasoning and the 1 January 2027 price change
- [ ] Update the README: setup section (new key, `llm.model` now required), the provider-abstraction paragraph (two providers, two different wire formats, no shared base), and the requirements list
- [ ] Confirm the account's real rate limits in AI Studio and correct this ADR's figures if they differ from ~10 RPM / 250K TPM / 1,500 RPD

**Validation (real calls, not simulated) — `books/test` only, and never `s1b translate`:**

Scoped exactly as ADR 015's validation was, and for the same reason: `translate` calls DeepL, not the LLM provider, and re-running it spends real DeepL character quota reserved for production books. `books/test/s1b/translated/es/zayagan-chp1.txt` already exists from a prior real translation and is reused as-is.

- [ ] `s1b cleanup` on `books/test/s1b/source/zayagan-chp1.txt` — first real Gemini call. English source, pre-translation, so this checks OCR-cleanup fidelity and whether the three existing normalizers still suffice or new artifacts appear
- [ ] `s1b ortho` and `s1b copyedit` against the **existing** translated fixture — the two Spanish, fidelity-critical tasks, and where `temperature: 0.0` against a model whose default is higher is most likely to show
- [ ] One large `single_chunk` task against a real oversized brief (`historia-expedicion-asia-vol3`'s 460,733-character brief, which chunks to 5 pieces) — the case Groq could never run; confirm both the per-chunk map calls and the reduce call succeed, and that no request-size guard is needed
- [ ] One small non-book task (`s5 evaluate` against an existing candidate brief) — confirms the non-book roots work unchanged
- [ ] Confirm `s3 dashboard` reports Gemini usage and shadow cost correctly, and specifically that `thoughtsTokenCount` is included in `completion_tokens` (compare a run's reported completion tokens against `usageMetadata` from a raw call, so the addition in Decision 4 is verified rather than assumed)
- [ ] Watch for and record any `finishReason` other than `STOP` during the above — especially `RECITATION` on the faithful-transcription tasks — and document what was seen either way
