# ADR 015 — Groq as a Second LLM Provider, and a Shared OpenAI-Wire Base

**Status:** Proposed.

---

## Context

### Why a second provider

The README has claimed since ADR 001 that the project is "LLM-agnostic by design" and that swapping providers "requires adding one file and one line in `config.yaml`". That claim has never been tested — `providers/llm/` has held exactly one provider since the beginning. Relying on a single upstream LLM provider for the whole pipeline is a concentration risk on its own terms, independent of anything that has or hasn't happened with Mistral specifically: pricing, rate limits, and model availability are all decisions made by a company this project has no influence over. Adding a second, independently-operated provider is worth doing now, on its own merits, and it's also the first real test of the provider-agnosticism the README has asserted since the beginning.

**Groqcloud.com** is the provider chosen for this: a free-tier API key is already available, and — as the next section covers — its wire format turns out to be close enough to what `providers/llm/mistral.py` already does that the integration is small.

### What's actually there today

Worth stating plainly, because it changes the shape of the work: **`providers/llm/mistral.py` is not an SDK integration.** It posts raw JSON to `https://api.mistral.ai/v1/chat/completions` with `httpx` and reads `choices[0].message.content` back. The only SDK-based provider in this repo is DeepL (`providers/translation/deepl.py`, via the `deepl` package).

Groq's endpoint is `https://api.groq.com/openai/v1/chat/completions` — the same OpenAI chat-completions wire format, deliberately so. The request payload (`model`, `messages`, `temperature`), the response shape (`choices[0].message.content`), the `usage` object (`prompt_tokens` / `completion_tokens` / `total_tokens`, plus timing fields this project ignores), and the error-body shape (`{"error": {"message": ...}}`, which `_error_detail()` already unwraps correctly) are all compatible with what `mistral.py` does today.

So the DRY-up anticipated as a later cleanup is available immediately, at the second provider rather than the third. That matters more than it usually would, because the code that would be duplicated is the retry/backoff/diagnostics loop — the single subtlest piece of code in this repository, hardened across two separate real production incidents (a `429` at chunk 51/118, and a mid-request network-interface failover at chunk 24). Two divergent copies of that is how the next hardening fix silently fails to reach one of them.

### The constraint that is not a drop-in

Groq's free tier for `openai/gpt-oss-120b` is bounded on four dimensions at once — requests per minute, requests per day, tokens per minute, tokens per day — and the first ceiling reached returns `429`. Per Groq's own rate-limit documentation the free-plan ranges are RPM 10–30, RPD 100–14.4K, **TPM 1.2K–15K**, TPD 3.6K–500K; secondary sources place `openai/gpt-oss-120b` specifically at roughly **30 RPM / 1,000 RPD / 8,000 TPM**. The per-model figures are not authoritative here and must be confirmed against the account's own dashboard (`console.groq.com/settings/limits`) — but the *order of magnitude* is not in doubt, and it is the order of magnitude that breaks things:

- **Twelve tasks set `max_chars: 100000` with `single_chunk: true`** (ADR 014). At a conservative ~3 characters per token for Spanish prose, one such request carries **~33,000 input tokens** before the system prompt is added. Against an 8,000 TPM ceiling that request is roughly **4× the entire per-minute budget**. A request larger than the whole per-minute allowance cannot succeed by waiting — the retry loop this project already relies on for `429` handling would spend its full 10-minute budget re-sending a request that was never admissible, then fail with a message about rate limits that reads as transient when it is structural.
- **TPM counts input *and* output.** An editorial task whose output is roughly the size of its input needs about double its input budget, so the usable input ceiling is nearer half the TPM figure.
- **The chunked System 1B tasks fare better but not well.** `cleanup`/`ortho`/`copyedit` use `llm_text`'s default `max_chars: 8000` — ~2,700 input tokens plus a ~2,300-character system prompt, call it ~5,500–6,000 tokens per round trip. That fits inside 8,000 TPM, but only about one chunk per minute. The book currently in production (`books/historia-expedicion-asia-vol3`, 901,457 characters at the `ortho`/`copyedit` stage) is **113 chunks**, or roughly 600,000 tokens for a single pass — around two hours of pure rate-limit waiting, and far beyond any plausible tokens-per-day allowance.

None of this is a reason not to integrate Groq. It is a reason to integrate it with the ceiling made explicit in code and config, rather than discovering it one exhausted retry budget at a time.

### The model

`openai/gpt-oss-120b` on Groq: 131,072-token context window, 65,536 max output tokens. Two model-specific parameters matter:

- **`reasoning_effort`** — `low` / `medium` / `high`, defaulting to `medium` for the GPT-OSS models. Reasoning tokens are billed as completion tokens and count against TPM. For this pipeline's work — orthographic correction, copyediting, structured expansion from a brief — reasoning is largely spent capacity.
- **`include_reasoning`** — a boolean; when true the response carries a separate `reasoning` field. GPT-OSS models use this rather than `reasoning_format` (the two are mutually exclusive, and `reasoning_format` is documented as unsupported for GPT-OSS).

---

## Decision

### 1. Extract a shared OpenAI-wire base, then build both providers on it

New file `providers/llm/openai_chat.py` holding `OpenAIChatProvider(LLMProvider)`: the chat-completions payload construction, the retry/backoff loop exactly as it exists today, `usage` accumulation, and `_error_detail()`. `providers/llm/mistral.py` and a new `providers/llm/groq.py` become thin subclasses supplying what genuinely differs.

Per-provider surface, as class attributes and overridable hooks:

| Hook | Mistral | Groq |
|---|---|---|
| `API_URL` | `https://api.mistral.ai/v1/chat/completions` | `https://api.groq.com/openai/v1/chat/completions` |
| `DISPLAY_NAME` | `Mistral` (error message prefix) | `Groq` |
| `RATE_LIMIT_HINT` | the existing tier-vs-usage text | free-tier dimensions + `console.groq.com/settings/limits` |
| `_extra_payload()` | `{}` | `reasoning_effort`, `include_reasoning: false` |
| `_log_rate_limit_headroom(response)` | existing `x-ratelimit-remaining` logic | Groq's two-dimensional headers (below) |
| `_is_fatal_rate_limit(response)` | `False` | see Decision 4 |

`providers/llm/base.py` stays as it is: the abstract `LLMProvider` interface, unchanged. `OpenAIChatProvider` is a *wire-format* base, not the project's LLM abstraction — a future Anthropic provider implements `LLMProvider` directly without inheriting anything from this file, since the Messages API is a different payload and response shape.

**This is a pure extraction for Mistral.** No behavioral change is intended, and the checklist below verifies that with a real Mistral call rather than by reading the diff. The retry budget (`_MAX_RETRY_SECONDS = 600`), the backoff shape, the `httpx.TransportError` handling, and the "raise the original network exception rather than a wrapped one" detail all move verbatim.

The rate-limit headers differ enough to justify the hook rather than a shared implementation. Mistral sends `x-ratelimit-remaining` (when it sends it at all — `mistral.py`'s own comment records that a real 429 arrived with no such header, corroborated by `pydantic/pydantic-ai#1885`). Groq sends a two-dimensional set with different names and a **duration-string** reset value:

```
x-ratelimit-limit-requests      integer, requests per day
x-ratelimit-remaining-requests  integer, requests per day
x-ratelimit-reset-requests      duration string, e.g. "2m59.56s"
x-ratelimit-limit-tokens        integer, tokens per minute
x-ratelimit-remaining-tokens    integer, tokens per minute
x-ratelimit-reset-tokens        duration string, e.g. "7.66s"
retry-after                     integer seconds, only present on a 429
```

Reusing Mistral's echo function unmodified would find no matching headers at all, and its `f"resets in {reset}s"` formatting would render `"resets in 2m59.56s s"` if pointed at Groq's. Groq's reset values are echoed as-is, not parsed.

### 2. Groq becomes the configured default

```yaml
llm:
  provider: groq
  model: openai/gpt-oss-120b
  temperature: 0.0
  reasoning_effort: low
  max_request_tokens: 4000
  pricing:
    prompt_per_million: 0.15
    completion_per_million: 0.60
```

`GROQ_API_KEY` joins `.env` / `.env.example`. `get_llm_provider()` gains a `groq` branch and its "Unknown LLM provider" message lists both.

Two hardcoded `"mistral-medium-latest"` fallbacks become wrong the moment a second provider exists and must go: [`providers/__init__.py:20`](../../providers/__init__.py#L20) and [`engines/llm_text.py:133`](../../engines/llm_text.py#L133). `model` becomes required — a missing `llm.model` raises a clear `ClickException` naming the config key, rather than silently sending a Mistral model name to whichever provider is configured. There is no defensible provider-agnostic default for that field.

`reasoning_effort` is set to `low`: this pipeline's tasks are transformation and structured expansion, not problem-solving, and reasoning tokens consume both budget and the TPM ceiling that Decision 3 exists to protect. It is a config key rather than a hardcoded constant because it is a genuine quality/cost dial of the same kind as `temperature`; providers that don't understand it ignore it.

`include_reasoning: false` is sent unconditionally by `GroqProvider` — belt and braces, since GPT-OSS is documented to put reasoning in a separate field rather than in `message.content`. As a defensive backstop, the provider raises a clear exception when `message.content` comes back empty or whitespace-only, instead of returning `""` for `llm_text` to write to disk as a plausible-looking empty output file.

**Temperature is left at `0.0` and flagged, not silently changed.** Groq's documentation recommends 0.5–0.7 for the GPT-OSS models, which contradicts this project's fidelity-first `0.0` default (`config.example.yaml`'s comment: "Recommended for editorial tasks where fidelity to the source text matters more than fluency"). Changing a global that every task inherits, at the same moment as changing the model underneath it, would make any resulting quality change impossible to attribute. Start at `0.0`, evaluate on `cleanup`/`ortho` where fidelity matters most, and adjust as a separate, deliberate step.

**Pricing is recorded at Groq's paid rates ($0.15 / $0.60 per million), not at zero.** Free-tier usage costs nothing today, so a literal cost column would read `$0.00` for every task. That is accurate and useless: a dashboard that reports zero exposure to a dependency the imprint now runs production through cannot warn about anything — the whole point of tracking cost per ADR 005 is visibility into what the pipeline actually depends on, free tier or not. The cost is a *shadow* cost — what this run would have cost if billed — documented as such in `config.example.yaml`. The tradeoff is real: ADR 011's "reported margin" then subtracts a cost that was never actually paid. That margin is already explicitly labeled as tracked-API-cost-only rather than `vsm.md`'s full title economics, so this widens an approximation that was already flagged rather than introducing a new lie — but it is a widening, and it is named here so it isn't rediscovered as a bug.

### 3. A request-size preflight, checked before any call is made

New optional `llm.max_request_tokens`. When set, `engines/llm_text.py` estimates each request's input size and refuses to send one that exceeds it — **before the completion loop**, at zero token cost, in exactly the shape ADR 014's `single_chunk` guard already established:

```python
if max_request_tokens:
    est = _estimate_tokens(system_prompt, largest_chunk)
    if est > max_request_tokens:
        raise click.ClickException(
            f"{prompt}: largest chunk is ~{est} tokens (system prompt + "
            f"{len(largest_chunk)} chars), over llm.max_request_tokens="
            f"{max_request_tokens}. Lower max_chars for this task, or raise "
            f"the limit if your account's tokens-per-minute allowance is "
            f"higher — see docs/adr/015-groq-llm-provider.md."
        )
```

Estimation is `(len(system_prompt) + len(text)) / 3.0`, a module constant with a comment, not a config key — three characters per token is deliberately conservative for Spanish prose, and adding a tunable for it would be config surface in exchange for precision the check doesn't need. The check is an early-warning heuristic; the server remains authoritative, which is what Decision 4 covers.

Two call sites, both in `llm_text.run()`:

1. **Over the chunks, before the loop** — catches the whole class of failure at zero cost, the same way and for the same reason as the `single_chunk` guard immediately above it.
2. **Before the reduce call, once the drafts exist** — ADR 014's reduce step sends the *concatenation of every map-step draft* as one user message, which can be larger than any individual chunk and whose size is unknowable in advance. The map tokens are already spent by then, but the reduce call's are not.

Omitting `llm.max_request_tokens` disables the check entirely and preserves today's behavior exactly, so this is additive for any account with headroom.

**What this does and does not fix.** It converts "burn a 10-minute retry budget, then fail with a message that reads as transient" into "fail immediately, name the numbers, name the remedy." It does **not** make the twelve `max_chars: 100000` tasks run on Groq's free tier. They still cannot run there. The remedies are lowering `max_chars` so ADR 014's map-reduce becomes the normal path (which needs the six missing `reduce_prompt` files written and tested — see Consequences) or a paid tier. Both are follow-up work, deliberately outside this ADR.

The recommended starting value of `4000` is half of the reported 8,000 TPM, reflecting that TPM counts input and output together and this pipeline's editorial tasks produce output of roughly input size. It should be re-derived from the account's actual dashboard figures.

### 4. A fatal `429` is not retried

Groq's `429` bodies name the dimension and the arithmetic — e.g. a message identifying tokens-per-minute with the limit, the amount used, and the amount requested. When the amount requested by a *single* request exceeds the limit itself, no amount of waiting helps: the request is inadmissible, not throttled.

`_is_fatal_rate_limit(response)` is a base-class hook returning `False` by default (preserving Mistral's behavior verbatim). `GroqProvider` overrides it to recognize that case from the response body and raise immediately — surfacing Groq's own diagnostic text, which is unusually good — instead of entering the backoff loop. Same principle as the previous pass's Mistral work: retry what is transient, fail fast and loudly on what is not, and never let the user watch a progress message for ten minutes on a request that could never have succeeded.

A per-day ceiling (`RPD`/`TPD`) is likewise not worth a ten-minute retry — the existing `elapsed + wait > _MAX_RETRY_SECONDS` check already gives up rather than sleeping past its budget when `retry-after` is large, so this needs no new mechanism, only a message that says what it means.

---

## Alternatives Considered

- **Copy the retry loop into `groq.py` and leave `mistral.py` untouched; DRY at the third provider.** Zero regression risk to freshly-hardened code, which is a genuine argument. Rejected: that loop absorbed two separate real production incidents' worth of hardening, and the failure mode of two copies isn't a visible bug — it's the *next* fix reaching one file and not the other, discovered months later on whichever provider didn't get it. The extraction is mechanical and verifiable by re-running a real Mistral task, which is a much smaller risk than that.

- **A single generic `openai_compatible` provider driven by a `base_url` in `config.yaml`.** Tempting — it would make provider number three a config edit with no new file. Rejected on two grounds. First, it discards exactly what the previous pass established as valuable: provider-specific diagnostics. Mistral's "tier is not usage" hint and Groq's four-dimensional rate limits are different explanations of the same status code, and a generic provider can only say "429". Second, `manifest.yaml` records `llm_provider` per task as permanent provenance; `openai_compatible` as a recorded value tells a future reader nothing about which company's model produced a published book's text.

- **Groq's official `groq` Python SDK.** Rejected: a new dependency for a single HTTP POST, and this project's whole retry/diagnostic layer operates at the HTTP level — status codes, response headers, `httpx.TransportError`. An SDK would wrap all three in its own exception taxonomy, and the hardening would have to be rewritten against that taxonomy for no gain. `mistral.py` set this precedent already, and it has held up.

- **Put `OpenAIChatProvider` in `providers/llm/base.py`, next to `LLMProvider`.** Rejected: mixes the project's abstract provider interface with one specific vendor wire format. A future Anthropic or local-Ollama provider implements the ABC without wanting any of the chat-completions specifics, and the two-method interface the README documents should stay readable as two methods.

- **Automatic failover — try Groq, fall back to Mistral on a fatal 429.** Rejected firmly. A run that silently switches models partway through produces a book whose chapters were written by different models, with a manifest recording only one of them. That destroys the provenance guarantee the manifest exists to provide and makes output non-reproducible, in exchange for convenience during exactly the situation where a human should be told what happened. Provider choice stays an explicit config decision, consistent with this project's standing "advisory outputs, decisions stay human" rule.

- **Per-task or per-book provider selection.** Deferred, not rejected — it is the natural next step and would have made the in-flight book's mixed provenance a non-issue (see Consequences). Out of scope here because it touches `task_loader`, the manifest schema, and `metrics.enrich()`'s assumption that `config["llm"]["provider"]` describes the run, which is three surfaces more than adding a provider needs.

- **Lower `max_chars` on the twelve `single_chunk` tasks as part of this ADR.** Rejected as scope. Six of those tasks have a tested `reduce_prompt` and would work; the other six (`newsletter`, `article-draft`, `s4 briefing`, `s4 content-strategy`, `s5 evaluate`, `s5 homeostat`) would hard-fail per ADR 014's Decision 2, and writing six reduce prompts is its own multi-pass job with its own real-call testing — this project's own history says every new prompt shape needs at least one round of hardening (ADR 002 points 6–7, ADR 006 points 9–10, ADR 014's three first-test defects). Bundling that into a provider integration would make both harder to verify.

- **Stay on Mistral alone and skip a second provider entirely.** Rejected: a project whose stated design property is provider-agnosticism should actually have more than one provider behind that abstraction, and Groq's free tier plus its close wire-format compatibility with what `mistral.py` already does makes this a low-cost moment to prove it out rather than a theoretical README claim.

---

## Consequences

**Easier:**

- The README's provider-agnosticism claim becomes demonstrated rather than asserted, and provider number three costs one small subclass instead of a duplicated retry loop.
- The retry/backoff/diagnostics hardening lives in one place, so the next incident's fix reaches every provider automatically.
- A rate-limit ceiling that would otherwise present as a mysterious hang followed by a confusing timeout now fails in under a second with the numbers and the remedy in the message.
- The pipeline has a genuine second provider option, at a per-token cost roughly a third of the Mistral rates currently in `config.example.yaml` if and when it moves to Groq's paid tier.

**Harder / needs care:**

- **The twelve `max_chars: 100000` tasks do not run on Groq's free tier.** That is most of System 1D's deliverables plus both System 4 tasks and both System 5 tasks — a large fraction of what this pipeline produces. Decision 3 makes the wall visible and cheap to hit; it does not remove it. Until `max_chars` comes down (and six `reduce_prompt` files get written) or the account moves to a paid tier, those tasks need Mistral, or a different provider, or money.
- **Prompt reliability does not transfer with the provider.** Every prompt in `prompts/` is Spanish, and every one was tuned against Mistral across multiple real-call iterations. `gpt-oss-120b` is a different, English-centric model. The three normalizers in `engines/llm_text.py` (`_DOUBLED_HEADING_RE`, `_CODE_FENCE_RE`, `_BRACKETED_HEADING_RE`) were each added in response to a *specific Mistral* failure mode; a new model may not exhibit those and will very likely exhibit others. Expect a full round of the pattern ADR 002/006/014 all record. Spanish editorial quality is the thing to check first and the thing least likely to be fine by default.
- **The book in production gets mixed provenance.** `books/historia-expedicion-asia-vol3` is mid-flight; flipping the global default mid-book means some of its stages were produced by `mistral-medium-latest` and the rest by `openai/gpt-oss-120b`. The manifest records this faithfully per task, so it is visible rather than hidden — but ADR 009's staleness cascade does not fire on a provider change (a provider change isn't an input change in the task graph's terms), so nothing flags the inconsistency. The cheap manual workaround is to point `config.yaml` back at Mistral for that book's remaining stages; the real fix is per-book provider selection, deferred above.
- **`llm.model` becomes required.** Any existing checkout whose `config.yaml` omits it — relying on the `mistral-medium-latest` fallback — now gets a clear error instead of a silent default. Intended, but it is a breaking config change and belongs in the README's setup section.
- **`llm.reasoning_effort` is a provider-specific key in a provider-generic block**, alongside `provider`/`model`/`temperature`/`pricing`. Mildly impure, and accepted for now on the grounds that the whole `llm:` block already switches wholesale when the provider does. If a third provider adds its own such key, that is the signal to nest per-provider config properly rather than accumulating flat keys.
- **Two providers means two accounts, two keys, and two sets of rate limits to keep in mind** when reading a `s3 dashboard` cost column whose figures now mean different things depending on which provider produced the row.

---

## Implementation Checklist

**Extraction (no behavior change):**

- [ ] Create `providers/llm/openai_chat.py` with `OpenAIChatProvider(LLMProvider)`: payload construction, the retry/backoff loop moved verbatim (budget, backoff shape, `httpx.TransportError` handling, original-exception re-raise), `usage` accumulation, `_error_detail()`, and the hooks `API_URL` / `DISPLAY_NAME` / `RATE_LIMIT_HINT` / `_extra_payload()` / `_log_rate_limit_headroom()` / `_is_fatal_rate_limit()`
- [ ] Reduce `providers/llm/mistral.py` to a subclass, keeping its existing rate-limit hint and header-echo logic as overrides, and preserving its explanatory comments (they record two real incidents' findings)
- [ ] Verify the extraction with a real Mistral call — `pipeline.py s1b cleanup books/test/s1b/source/zayagan-chp1.txt` (9,829 chars, one chunk) — confirming identical behavior, not just an identical-looking diff.

**Groq provider:**

- [ ] Create `providers/llm/groq.py`: endpoint, hint text, `reasoning_effort` + `include_reasoning: false` in `_extra_payload()`, Groq's two-dimensional header echo (reset values echoed as duration strings, not parsed as floats), `_is_fatal_rate_limit()` per Decision 4, and the empty-`content` guard
- [ ] Add the `groq` branch to `get_llm_provider()` with a `GROQ_API_KEY` check mirroring the Mistral one; update the "Unknown LLM provider" message to list both
- [ ] Remove both `"mistral-medium-latest"` fallbacks ([`providers/__init__.py:20`](../../providers/__init__.py#L20), [`engines/llm_text.py:133`](../../engines/llm_text.py#L133)); raise a clear `ClickException` naming `llm.model` when it is absent
- [ ] Add `GROQ_API_KEY` to `.env.example`

**Preflight:**

- [ ] Add `_estimate_tokens()` and the `llm.max_request_tokens` check to `engines/llm_text.py` — over the chunks before the completion loop, and again over the joined drafts before the reduce call
- [ ] Confirm the check is a no-op when `llm.max_request_tokens` is absent

**Config and docs:**

- [ ] Update `config.example.yaml`: `groq` defaults, `reasoning_effort`, `max_request_tokens` (with the "half your TPM, because TPM counts output too" reasoning), and Groq's paid rates in `pricing:` with the shadow-cost caveat spelled out
- [ ] Update `config.yaml` to `provider: groq` / `model: openai/gpt-oss-120b`
- [ ] Update the README: setup section (`GROQ_API_KEY`, `llm.model` now required), the provider-abstraction paragraph, and a note on the free-tier ceiling versus `max_chars: 100000` tasks
- [ ] Confirm the account's real limits at `console.groq.com/settings/limits` and correct this ADR's figures if they differ from the 30 RPM / 1,000 RPD / 8,000 TPM assumed here

**Validation (real calls, not simulated) — scoped narrowly, `books/test` only, no full pipeline run:**

Validation uses the existing `books/test` fixture and touches only the specific `s1b` tasks needed to exercise a real Groq call under fidelity-sensitive conditions. It deliberately does **not** run `s2 run`, `s1d`, `s4`, or `s5` against any book — those are out of scope for a provider swap and would just add noise to what's being checked here. Most importantly: **`s1b translate` is not run at any point in this validation.** `translate` calls DeepL, not the LLM provider this ADR touches, and re-running it against the same fixture spends real DeepL character quota for no purpose the LLM provider work needs — that quota is reserved for actual books in production (see the earlier `historia-expedicion-asia-vol3-part1` DeepL-quota split). `books/test/s1b/translated/es/zayagan-chp1.txt` already exists from a prior real translation and is reused as-is.

- [ ] `s1b cleanup` on `books/test/s1b/source/zayagan-chp1.txt` — first real Groq call. This task runs on the raw **English** source, before translation, so it's checking OCR-cleanup fidelity and the three existing normalizers (`_DOUBLED_HEADING_RE`, `_CODE_FENCE_RE`, `_BRACKETED_HEADING_RE`), not Spanish quality.
- [ ] `s1b ortho` and `s1b copyedit`, each run directly against the **existing** `books/test/s1b/translated/es/zayagan-chp1.txt` — not against a fresh `translate` run. These are the two Spanish-language, fidelity-critical tasks (`temperature: 0.0` against Groq's 0.5–0.7 recommendation is most likely to show here) and don't require `translate` to have just run, only for its prior output to already be on disk, which it is.
- [ ] One `single_chunk` task against a real oversized input, confirming the preflight fires immediately with the numbers named and no tokens spent
- [ ] One deliberately oversized single request with the preflight disabled, confirming `_is_fatal_rate_limit()` raises at once instead of entering the retry loop
- [ ] Confirm `s3 dashboard` reports Groq token usage and shadow cost correctly, and that a book with rows from both providers reads sensibly
