# ADR 021 — Z.ai (GLM-4-Flash) as the Default LLM Provider

**Status:** Proposed (2026-09-29). Design only — no code changes ship with this PR. No `ZAI_API_KEY`
exists yet, so nothing in this ADR has been validated against a real call; every number carried
over from research is explicitly flagged as such below.

---

## Context

Google AI Studio's free tier has now cost two real production runs on `the-gobi-desert`: a
sustained `503 UNAVAILABLE` that ADR 020's model-fallback couldn't route around because every
sibling Flash model was saturated at the same moment, followed by a deterministic
`blockReason: PROHIBITED_CONTENT` on ordinary travelogue text (children collecting relics at a
lake, a deaf-mute temple helper, a Tungan host's household — nothing that reads as prohibited).
Mistral, the provider Google AI Studio itself replaced as default (ADR 017), is currently
unreachable for a new paid signup — the account owner's own words: "Mistral won't allow me to
subscribe as its website keeps crashing." Both of this project's two shipped LLM providers are
presently unusable for new or continued production work, for two unrelated reasons.

The user has chosen **Z.ai**, running its free **GLM-4-Flash** model, as the pipeline's new
default provider. Two research findings shape this design:

### 1. Z.ai's chat-completions endpoint is OpenAI-wire-compatible

Confirmed directly against `docs.z.ai`'s own API reference: `POST
https://api.z.ai/api/paas/v4/chat/completions`, `Authorization: Bearer <key>` — the same request/
response shape `providers/llm/mistral.py` already speaks (`model`/`messages`/`temperature` in,
`choices[0].message.content` + a `usage` object out). This makes Z.ai the **second** OpenAI-wire
provider actually present in this codebase today. The first was Groq (ADR 015/016) — added,
validated against real calls, and then **fully reverted** (ADR 015's own status note) when its
Developer tier closed to new signups. That revert undid the shared-base extraction ADR 015 built
(`providers/llm/openai_chat.py`) along with it: `mistral.py` today is back to a single,
fully self-contained file with no shared base anywhere in `providers/llm/`.

`providers/llm/google_aistudio.py` carries this comment, written when Mistral and Google AI
Studio were the only two providers and already anticipating this moment:

> A third LLM provider is the trigger to extract this into a shared plain function
> (url/headers/payload/display-name) — not a class hierarchy...

Z.ai is, in the plain count of providers this project has actually built, the **third** (Mistral,//
Groq, Google AI Studio, now Z.ai) — even though only two of those four ever shared this wire
format. Per explicit direction from the account owner, weighed against the real risk that the
last attempt at this exact extraction was thrown away days later: **this ADR extracts a shared
OpenAI-wire retry helper now**, accepting that risk a second time because the reason the first
attempt was discarded (Groq's tier closing) had nothing to do with whether the extraction itself
was sound — it was sound, and reverting it was simply the cost of reverting everything about
Groq at once.

### 2. GLM-4-Flash's free-tier limitation is a soft throttle, not a hard reject

The account owner's own quoted text, carried into this ADR as reported and explicitly flagged
**unverified against Z.ai's public docs**:

> "To ensure stable access to GLM-4-Flash during the free trial, requests with context lengths
> over 8K will be throttled to 1% of the standard concurrency limit."

This could not be independently re-confirmed during this ADR's research. Z.ai's account-specific
rate-limit page (`z.ai/manage-apikey/rate-limits`) requires a login this project doesn't have
yet; Z.ai's public API reference confirms the endpoint and auth shape but not this figure; and
third-party sources found during research were inconsistent with each other and are not trusted
on their own, per this project's own standing rule about secondary sources (ADR 017's real,
material RPD correction — trusting a blog over the vendor's own model card was wrong once
already). This figure is recorded here exactly as the sole reason this ADR exists to record it —
to be reconfirmed for real the moment a `ZAI_API_KEY` exists — the same discipline already
applied to `config.example.yaml`'s `pricing`/`limits` blocks.

What matters architecturally, regardless of the exact number: **this is a soft slowdown, not
Groq's hard reject-over-the-limit wall.** Groq's TPM ceiling returned a fast, clean `413` for an
inadmissible request (ADR 015 Decision 4) — nothing to wait for, fail immediately, name the
remedy. A request over Z.ai's context threshold does not error; it queues, for however long "1%
of standard concurrency" turns out to mean in practice. That is structurally the same failure
shape this project has already been burned by **twice this month**: Google AI Studio's fixed
120-second `httpx` timeout on a response that legitimately takes longer than 120 seconds to
generate (ADR 017/018's open timeout finding, and the real `the-gobi-desert` incident that
motivated ADR 020). The fix, per the account owner's explicit direction, is pre-emptive this
time: keep requests small enough to stay under the throttle threshold by default, and give this
new provider a client timeout generous enough that even a legitimately slow response isn't
mistaken for a hung connection.

**No `ZAI_API_KEY` exists yet.** This ADR is design plus code; real-call validation (the standard
every provider added so far has met — ADR 015, 017, 020) is explicitly deferred to a follow-up
pass once a key exists, named plainly in the Implementation Checklist rather than glossed over.

---

## Decision

### 1. Extract a shared OpenAI-wire retry helper, `providers/llm/openai_chat.py`

`OpenAIChatProvider(LLMProvider)` — an extraction of `mistral.py`'s current retry/backoff loop
(time-based budget, exponential backoff capped per-wait, `httpx.TransportError` handling with the
original exception re-raised rather than wrapped, `usage` accumulation, `_error_detail()`'s
best-effort error-body unwrapping) into a base class with overridable hooks:

| Hook | Mistral | Z.ai |
|---|---|---|
| `API_URL` | `https://api.mistral.ai/v1/chat/completions` | `https://api.z.ai/api/paas/v4/chat/completions` |
| `DISPLAY_NAME` | `Mistral` | `Z.ai` |
| `REQUEST_TIMEOUT` | `120.0` (unchanged) | `300.0` — see Decision 4 |
| `RATE_LIMIT_HINT` | existing tier-vs-usage text, verbatim | 8K-context/1%-concurrency text, flagged unverified |
| `_extra_payload()` | `{}` | `{}` (no model-specific params known yet) |
| `_log_rate_limit_headroom(response)` | existing `x-ratelimit-remaining` logic, verbatim | no-op until a real response body/headers are seen |
| `_is_fatal_rate_limit(response)` | `False` (default, unchanged) | `False` (default) — see Decision 5 |
| `_extra_error_hint(response)` | none | none yet |

This is almost exactly ADR 015's Decision 1 table. That design was sound; what got reverted was
Groq's *usability*, not the shape of this extraction. `providers/llm/base.py` — the project's own
abstract `LLMProvider` interface — is untouched, same reasoning as ADR 015: this is a
*wire-format* base, not the pipeline's provider abstraction. `providers/llm/google_aistudio.py`
is **not touched at all**: Gemini's native `generateContent` remains a genuinely different wire
format (ADR 017 Decision 5), unaffected by anything here.

### 2. `providers/llm/mistral.py` becomes a thin subclass — pure extraction, no behavior change

Verified the same way ADR 015 verified it the first time: a real Mistral call, before and after,
confirmed to produce identical retry/backoff sequences and failure messages. Every existing
comment recording a real incident (the 429 at chunk 51/118, the mid-request network failover at
chunk 24) moves into the relevant override hook verbatim, not paraphrased — those comments are
the record of why this loop looks the way it does, and rewriting them risks losing that.

### 3. New file `providers/llm/zai.py` — `ZaiProvider(OpenAIChatProvider)`

- `model` stays a required, provider-agnostic `llm.model` config value — no hardcoded fallback,
  matching `get_llm_provider()`'s existing rule that a wrong-provider model name should fail
  loudly, not silently. Default value in config: `glm-4-flash`.
- `RATE_LIMIT_HINT` states the 8K/1% figure exactly as quoted above, explicitly marked as
  user-reported and unverified against Z.ai's own public docs, and points at
  `z.ai/manage-apikey/rate-limits` (the account dashboard) as where to check the real, current
  number.
- No `_is_fatal_rate_limit()` override yet. Groq's equivalent hook exists because a real call
  found a genuinely inadmissible request returns a distinct, non-retryable status (`413`) — that
  finding doesn't exist for Z.ai yet. Start with the base class's default (always retry a
  retryable status); add an override only if a real call ever shows a structurally-fatal case.

### 4. `REQUEST_TIMEOUT` is a per-provider hook on the shared base, not a shared constant

Mistral keeps its proven `120.0` unchanged — this is the one place the extraction is not
behavior-preserving by design, and it's named as such rather than folded silently into "no
behavior change." `ZaiProvider` sets `300.0` (2.5× Mistral's), a deliberate response to Context
point 2's soft-throttle risk: a request that queues under reduced concurrency rather than being
rejected needs real headroom before the client gives up and retries a request Z.ai may still be
processing — exactly the mechanism ADR 017/018/020 already had to reason through for Google AI
Studio's fixed timeout, applied here pre-emptively instead of after a real incident.
`300.0` is a first, reasoned number, not a confirmed one — re-derive it once real response times
under Z.ai's throttle are observed.

### 5. A conservative, provider-aware default `max_chars` for Z.ai

ADR 018's existing `_resolve_default_max_chars()` derives a default from `llm.limits.
max_output_tokens` — an **output**-side ceiling. GLM-4-Flash's constraint is an **input/context**
soft-throttle threshold, a different shape entirely; reusing ADR 018's knob would derive a
number sized for the wrong problem. This ADR adds a new optional `llm.limits.max_context_chars`
key. When `llm.provider` is `z-ai` and this key is absent, `_resolve_default_max_chars()` falls
back to a hardcoded conservative default — proposed **`6000` characters**, comfortably under an
8,000-token context even given ADR 015's own real finding that this project's ~3-characters-per-
token local estimate can run roughly 1.6× off from a real tokenizer in either direction. Every
task's explicit `max_chars` in `tasks.yaml` still wins outright and unconditionally, unchanged
from ADR 018 Decision 3 — **no `tasks.yaml` edits ship with this ADR** (see Consequences for why
that's a real, named gap, not an oversight).

### 6. Z.ai becomes the configured default

```yaml
llm:
  provider: z-ai
  model: glm-4-flash
  temperature: 0.0
  limits:
    # UNVERIFIED — see Context, point 2. GLM-4-Flash's free tier throttles requests whose
    # context exceeds ~8K to 1% of standard concurrency (soft slowdown, not a hard reject).
    # 6000 chars is a conservative first guess at staying under that; confirm and adjust once
    # real usage against a real key shows actual token counts and response times.
    max_context_chars: 6000
  pricing:
    prompt_per_million: 0.0
    completion_per_million: 0.0
```

`pricing` at a literal `0.0`, not a shadow paid-tier rate, is a deliberate departure from both
existing precedents: ADR 015 recorded Groq's shadow cost at its real paid rate because Groq's
free tier was explicitly a time-limited allowance in front of paid usage; ADR 017 did the same
for Gemini, whose promotional pricing has a named, dated cutover (1 January 2027) after which the
"shadow" cost becomes a real one. GLM-4-Flash, as researched, is documented as a standing free
model rather than a time-limited trial with a known graduation date — so there is no real dollar
figure being shadowed. This is flagged in the same config comment for re-confirmation once the
user has an account, in case Z.ai's actual terms differ from what's assumed here.

`ZAI_API_KEY` joins `.env.example` (`your-zai-api-key-here`, matching the existing
`MISTRAL_API_KEY`/`GOOGLE_AISTUDIO_API_KEY`/`DEEPL_API_KEY` naming convention exactly) and
`get_llm_provider()`'s new `z-ai` branch, checked the same way as the other two providers.

### 7. README and provider-abstraction documentation

The `providers/` architecture paragraph, the Requirements section's provider-key bullet, and the
"swapping providers" contribution-guidance paragraph each get the same treatment ADR 015, 017,
and 020 already gave them — Z.ai added to the list, the shared OpenAI-wire base mentioned
alongside the existing "why Gemini stays separate" reasoning.

---

## What does NOT ship in this ADR — explicit, not silent

- **No real-call validation of anything.** No `ZAI_API_KEY` exists. Every number above is a
  reasoned default or a user-reported, unverified figure — the Implementation Checklist marks
  every real-call item as pending, not done.
- **No Groq-style hard preflight/reject mechanism** (ADR 015 Decision 3's `max_request_tokens`).
  Not needed: GLM-4-Flash's constraint is a soft slowdown, not a hard wall a request can fail
  against, so there's nothing structurally invalid to preflight-reject against. If real testing
  ever finds a genuine hard ceiling (a Groq-413-shaped case), that's a fast, scoped follow-up.
- **No `tasks.yaml` changes.** `ortho`/`copyedit`'s Gemini-era `max_chars: 40000` (added for
  Google AI Studio's timeout problem, see the earlier `the-gobi-desert` incident) stays exactly
  as it is. Because an explicit `max_chars` always wins over the provider-aware default (ADR 018
  Decision 3, unchanged here), those two tasks will **not** pick up Decision 5's new
  `max_context_chars`-derived default automatically — running them against Z.ai without also
  lowering that number is expected to recreate exactly the risk Decision 4's longer timeout is
  meant to guard against. Named here so it isn't rediscovered as a surprise; fixing it is real
  follow-up work, not bundled into this design pass.
- **No per-system or per-book provider override.** Confirmed against `lib/task_loader.py`'s
  `_RESERVED_KEYS` and `lib/orchestrator.py`: no such mechanism exists anywhere in this codebase.
  `llm.provider`/`llm.model` remain single global settings for the whole pipeline, matching the
  account owner's explicit request to keep "the same pattern" — this ADR introduces no new
  config axis.

---

## Alternatives Considered

- **Keep `zai.py` fully self-contained, duplicating Mistral's retry loop** (i.e. Decision 1
  reversed). This was the account owner's default-safe option going in — zero risk to Mistral's
  already-hardened code, and it sidesteps repeating an extraction that was thrown away once
  already. Explicitly not chosen: the account owner weighed both options directly and chose
  extraction, on the grounds that ADR 015's extraction itself was never the problem — only
  Groq's usability was — and that this project's own code already names "a third provider" as
  the moment to do this.
- **Reuse ADR 018's `llm.limits.max_output_tokens`-derived default for Z.ai instead of a new
  `max_context_chars` key.** Rejected: that key answers "how much can the model *output*
  before truncating," a completely different question from "how large an *input* triggers
  Z.ai's throttle." Reusing it would either derive a number with no real connection to the
  actual constraint, or require overloading one config key with two unrelated meanings depending
  on which provider is active — worse than one small, honestly-named new key.
- **A Groq-style hard preflight check against `max_context_chars`, refusing an over-threshold
  request before sending it.** Deferred, not rejected — Decision 5's conservative default should
  keep every normal chunk under the threshold by construction, so a hard preflight would mostly
  guard against a hand-edited `tasks.yaml` `max_chars` that's too large for this provider. Worth
  adding once real usage shows whether that actually happens in practice; building it now against
  an unconfirmed 8K figure risks tuning a check against a number that turns out wrong.
- **Recording Z.ai's shadow cost at some nonzero rate "just in case," mirroring Groq/Gemini.**
  Rejected for now (Decision 6) — there is no known paid rate to shadow. Revisit if Z.ai's terms
  turn out to include one.
- **Wait for Mistral's website to recover, or wait for Google AI Studio's 503s to clear, instead
  of adding a third provider at all.** Rejected per the account owner's explicit direction: both
  are currently blocked for reasons outside this project's control, and Z.ai adding real optionality
  (a third, independently-operated provider) is worth doing on its own merits regardless of
  whether either blocker resolves on its own.

---

## Consequences

**Easier:**
- A production run has a path forward the moment a `ZAI_API_KEY` exists, independent of both of
  the other two providers' current availability problems.
- The shared OpenAI-wire retry helper means the next hardening fix to that loop (transient
  network failure, a new retryable status code) reaches Mistral and Z.ai together automatically,
  instead of needing to be found and re-applied twice.
- GLM-4-Flash costs nothing on the free tier, and this ADR records that honestly (Decision 6)
  rather than inventing a shadow figure with no basis.

**Harder / needs care:**
- **Mixed extraction risk, accepted a second time.** The last time this exact shared-base
  extraction was built (ADR 015), it was discarded days later — not because the extraction was
  wrong, but because Groq itself became unusable. That risk is structurally the same here: if
  Z.ai turns out unusable too, this extraction either gets reverted a second time or is kept for
  Mistral's sake alone. Named plainly rather than assumed away.
- **`ortho`/`copyedit`'s existing `max_chars: 40000` silently does not benefit from this ADR's
  new conservative default** (see "What does NOT ship," above) — running Z.ai against those two
  specific tasks without a manual `tasks.yaml` adjustment risks recreating the exact slow-request/
  timeout problem this ADR's Decision 4 tries to get ahead of.
- **Every number specific to Z.ai/GLM-4-Flash in this ADR is unverified against a real account**
  — the 8K/1% figure, the `300.0`s timeout, the `6000`-character default, and the "no paid tier"
  pricing assumption. All four are named as such, all four need re-confirmation once a key
  exists, and none of them should be read as settled facts in the meantime.
- **Three providers now means three accounts, three keys, and three sets of provider-specific
  behavior to hold in mind** when reading a `s3 dashboard` row — same caution ADR 015 already
  named for two.

---

## Implementation Checklist

**Extraction (no behavior change except Decision 4's timeout):**

- [ ] Create `providers/llm/openai_chat.py` with `OpenAIChatProvider(LLMProvider)`: payload
      construction, the retry/backoff loop moved verbatim (budget, backoff shape,
      `httpx.TransportError` handling, original-exception re-raise), `usage` accumulation,
      `_error_detail()`, and the hooks `API_URL` / `DISPLAY_NAME` / `REQUEST_TIMEOUT` /
      `RATE_LIMIT_HINT` / `_extra_payload()` / `_log_rate_limit_headroom()` /
      `_is_fatal_rate_limit()` / `_extra_error_hint()`
- [ ] Reduce `providers/llm/mistral.py` to a subclass, `REQUEST_TIMEOUT = 120.0`, every existing
      comment and hint preserved verbatim in the relevant hook
- [ ] Verify the extraction with a real Mistral call, confirming identical behavior to today's
      `mistral.py`, not just a structurally-similar diff

**Z.ai provider:**

- [ ] Create `providers/llm/zai.py`: `ZaiProvider(OpenAIChatProvider)`, `REQUEST_TIMEOUT = 300.0`,
      the 8K/1%-concurrency `RATE_LIMIT_HINT` text (explicitly marked unverified in the string
      itself, pointing at the account dashboard)
- [ ] Add the `z-ai` branch to `get_llm_provider()` with a `ZAI_API_KEY` check mirroring the
      existing two; update the "Unknown LLM provider" message to list all three
- [ ] Add `ZAI_API_KEY` to `.env.example`

**Provider-aware chunk sizing:**

- [ ] Add `llm.limits.max_context_chars` handling to `engines/llm_text.py`'s
      `_resolve_default_max_chars()`: when `llm.provider == "z-ai"`, use `max_context_chars` if
      set, else the hardcoded `6000` fallback; every other provider's behavior is unchanged
- [ ] Confirm an explicit per-task `max_chars` in `tasks.yaml` still overrides this unconditionally

**Config and docs:**

- [ ] Update `config.example.yaml`: `z-ai` defaults, `max_context_chars` with the unverified
      caveat spelled out in the comment, `pricing: 0.0`/`0.0` with the "no known paid tier"
      rationale
- [ ] Update `config.yaml` to `provider: z-ai` / `model: glm-4-flash`
- [ ] Update the README: Requirements section (`ZAI_API_KEY`), the provider-abstraction
      paragraph, the contribution-guidance paragraph
- [ ] Stubbed-`httpx` unit tests (stdlib `unittest`, matching `tests/test_google_aistudio_
      fallback.py`'s existing pattern): Mistral's retry/backoff behavior is unchanged after
      extraction; Z.ai's longer timeout is actually used; the rate-limit hint text appears on a
      retryable status; `_resolve_default_max_chars()`'s new Z.ai branch resolves correctly with
      and without `max_context_chars` set, and is unaffected for every other provider

**Explicitly deferred to a follow-up pass, once `ZAI_API_KEY` exists:**

- [ ] One real Z.ai call against the `books/test` fixture, confirming the model ID, response
      shape, and usage reporting work as designed
- [ ] Confirm or correct the 8K-context/1%-concurrency figure against the real account dashboard
- [ ] Confirm or correct `REQUEST_TIMEOUT = 300.0` and `max_context_chars: 6000` against real
      observed response times and token counts
- [ ] A short real quality comparison against Mistral/Gemini output, on the fidelity-critical
      `ortho`/`copyedit` tasks specifically (same standard ADR 015 applied to Groq)
- [ ] Decide whether/how to adjust `ortho`/`copyedit`'s existing `max_chars: 40000` for Z.ai
      specifically (see Consequences)
