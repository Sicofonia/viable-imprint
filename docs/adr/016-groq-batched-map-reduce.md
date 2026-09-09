# ADR 016 — A Batched Map-Reduce Strategy to Unblock the Large-Input Tasks (S1D Expansion, S4, S5)

**Status:** Proposed.

---

## Context

ADR 015 made Groq (`openai/gpt-oss-120b`) the default LLM provider and validated it end-to-end for System 1B (`cleanup`, `ortho`, `copyedit`), but explicitly documented that twelve `single_chunk` tasks (ADR 014) — six in System 1D, two in System 4, two in System 5, plus `newsletter`/`article-draft` — cannot run on Groq's free tier, because their requests exceed its confirmed 8,000-token-per-minute ceiling for this model. ADR 015 named three possible remedies and deferred all of them: point those tasks back at Mistral, move them to a different provider, or upgrade to a paid Groq tier.

**Mistral is not currently available as a fallback provider.** That closes the first remedy outright — there is currently no working LLM provider for any of the twelve tasks.

**Groq's Developer tier — this ADR's first draft recommended upgrading to it — is not currently available either.** Signups have been closed since early August 2026 due to industry-wide compute demand, with only Enterprise tier open and no visible waitlist or reopening date (confirmed via [Groq's own community forum](https://community.groq.com/t/unable-to-upgrade-to-developer-tier/747) and independent reports). This isn't a brief blip to wait out; Enterprise-tier pricing and commitments aren't a fit for a two-person imprint either. So this ADR needs a fix that doesn't depend on any account tier being purchasable — see Alternatives Considered for why the tier upgrade is kept on record rather than dropped entirely.

### Not all twelve tasks are equally broken — checked against real data, not assumed

**Six tasks read `s1d brief`, whose size scales with the whole manuscript:** `synopsis`, `story-map`, `one-pager`, `press-dossier`, `trailer-storyboard`, `goodreads-profile`. The one full-length book processed so far, `historia-expedicion-asia-vol3`, produced a **460,733-character** brief — the exact case ADR 014 was written to handle. At this task family's own `max_chars: 100000`, that chunks into 5 pieces (ADR 014's own confirmed count), each landing at roughly **19,000–31,000 tokens** depending on whose count is trusted: this project's own conservative `~3 chars/token` preflight estimate, or Groq's real tokenizer, extrapolated from the one confirmed real ratio ADR 015's Implementation notes captured (~4.7 chars/token, from a real 413 response). Either way, that's well over Groq's free-tier ceiling for a single request. All six already have ADR 014's map-reduce built (a `reduce_prompt` per task), so it's specifically the **map step** (each chunk, one request per chunk) that's blocked. The **reduce step** — one call reconciling the per-chunk drafts — is small for this particular book (5 drafts), but not necessarily always: `story-map`'s own per-chunk output is a waypoint list, which scales with narrative detail, not just chunk count, so a longer or more event-dense future book could plausibly push even the reduce call past a tight ceiling too. These six are genuinely, structurally incompatible with Groq's free tier for any book of meaningful length, and a fix needs to hold for an arbitrarily long book, not just the one seen so far.

**The other six read a small, bounded document, not a manuscript:** `newsletter` (a month's production digest), `article-draft` (one content-strategy brief), `s4 briefing` (one scan digest), `s4 content-strategy` (one briefing), `s5 evaluate` (one candidate description), `s5 homeostat` (one S3/S4/decision-log confrontation). Checked directly against every real instance this project has produced so far (`intelligence/`, `candidates/`, `homeostat/`, `newsletter/`):

| Task's real input | Real sizes observed | System prompt size | Estimated tokens (this project's `~3 chars/token`) |
|---|---|---|---|
| `s4 briefing`'s `scan` digest | 522 – 13,655 chars (5 samples) | 3,000 chars | up to **~5,550** (largest sample, 2026-07-26) |
| `s4 content-strategy`'s briefing | 3,955 – 6,100 chars | 3,000 chars | up to ~3,050 |
| `s5 evaluate`'s candidate brief | 676 – 3,895 chars (4 samples) | 6,786 chars | up to ~3,560 |
| `s5 homeostat`'s confrontation doc | ~1,060 – 1,173 chars | 3,231 chars | ~1,470 |
| `newsletter`'s scan digest | 3,692 – 3,764 chars | 3,934 chars | ~2,570 |
| `article-draft`'s content-strategy brief | 1,407 – 2,015 chars | 1,843 chars | ~1,290 |

None of these has ever come remotely close to the defensive `max_chars: 100000` ceiling those tasks were given, and every real sample comfortably clears Groq's confirmed 8,000 TPM free-tier limit too — **with one exception: this project's own `llm.max_request_tokens` preflight** (ADR 015, currently `4000`, deliberately conservative to cover this project's estimate running high against Groq's real tokenizer). The largest real `s4 briefing` input on record (13,655 characters, 2026-07-26) estimates to ~5,550 tokens by this project's own heuristic — over the current `4000` threshold, and would very likely be rejected by the *local* preflight as a false positive, not by Groq itself. **This distinction matters directly for this ADR's design, not just as a footnote:** none of these six tasks has a `reduce_prompt`, so none of them can safely be chunked — if a fix for the six *large* tasks below were applied indiscriminately to all twelve, it would make these six *worse*, forcing `s4 briefing`'s edge case into more than one chunk and hitting ADR 014's hard-failure guard instead of the (probably unnecessary) local preflight rejection it hits today.

---

## Decision

Build a **batched map-reduce strategy**, structured the same way ADR 015 structured provider differences: a base implementation that is a byte-for-byte extraction of today's behavior (safe for Mistral, or any future high-ceiling provider, completely untouched), and a Groq-specific override that does the new work. Applied *only* to the six large S1D tasks — the six small tasks keep their current, unaffected behavior entirely.

### 1. Generalize `lib/chunker.py`'s grouping logic

`chunk_by_paragraphs()`'s core loop — accumulate items until adding the next would exceed `max_chars`, then start a new group — doesn't care that its items happen to be paragraphs. Extract it into `group_by_size(items: list[str], max_chars: int, joiner: str = "\n\n") -> list[str]`, taking any list of strings and returning size-bounded, joined groups. `chunk_by_paragraphs(text, max_chars)` becomes a one-line wrapper: `group_by_size(text.split("\n\n"), max_chars)`. Verified as a pure refactor — every existing task's chunking behavior must come out byte-identical, checked against a real or synthetic long input before/after, the same discipline ADR 015 used for `openai_chat.py`'s extraction.

This is what makes the new strategy possible without duplicating grouping logic: the same primitive that chunks a manuscript into pieces also groups map-step *drafts* into reduce-safe batches (Decision 2).

### 2. A `MapReduceStrategy` class hierarchy in a new `lib/map_reduce.py`

```python
class MapReduceStrategy:
    def effective_chunk_chars(self, task_max_chars: int) -> int:
        return task_max_chars  # unchanged by default

    def reduce(self, llm, reduce_system_prompt: str, drafts: list[str]) -> str:
        # today's exact behavior: one call, every draft wrapped in
        # <borrador numero="i" total="N"> and concatenated.
        ...


class SingleCallMapReduce(MapReduceStrategy):
    """Today's behavior, given an explicit name rather than left as just
    'the base class's behavior' — this is what Mistral (or Groq, if
    Developer tier ever reopens) uses. Byte-for-byte what
    engines/llm_text.py already does; see Decision 3 for why nothing about
    it changes.
    """


class BatchedMapReduce(MapReduceStrategy):
    """For a provider with a hard per-request token ceiling. Takes a
    character budget (derived from llm.max_request_tokens, the same
    estimate this project's preflight already uses — see Decision 4).
    """
    def __init__(self, budget_chars: int):
        self._budget_chars = budget_chars

    def effective_chunk_chars(self, task_max_chars: int) -> int:
        return min(task_max_chars, self._budget_chars)

    def reduce(self, llm, reduce_system_prompt: str, drafts: list[str]) -> str:
        while len(drafts) > 1:
            batches = group_by_size(drafts, self._budget_chars)  # each batch pre-joined
            drafts = [llm.complete(reduce_system_prompt, batch) for batch in batches]
        return drafts[0]
```

`BatchedMapReduce.reduce()` groups drafts into batches that fit the budget via `group_by_size()`, reduces each batch with the *same* `reduce_prompt` (producing one intermediate draft per batch), and recurses on the resulting drafts until exactly one remains. This is what handles an arbitrarily long book: more chunks produce more first-round batches, which produce more intermediate drafts, which get reduced again in the next round — the *number of rounds* grows (slowly — logarithmically in the number of drafts), but no single call's input ever exceeds the budget, regardless of book length. This is also what closes the "reduce-step aggregate-size risk" the Context section names for `story-map`: today's single, unconditional reduce call over every draft is exactly what breaks on a long or detail-dense book; a budget-respecting batched reduce doesn't. (A batch that happens to contain exactly one draft still goes through one reduce call above — a minor, non-load-bearing optimization to skip that could be added during implementation, but isn't needed for correctness and would only complicate this sketch.)

### 3. Providers supply their own strategy, optionally — engines stay provider-agnostic

New optional method, following the *exact* convention `translate_document()` already established for `TranslationProvider` (ADR 001/005: not an `@abstractmethod`, checked with `getattr()` at the call site, a provider that doesn't implement it just gets the default): `get_map_reduce_strategy(max_request_tokens: int | None) -> MapReduceStrategy`.

**`MistralProvider` needs zero changes — not even a default override.** `engines/llm_text.py` does `getattr(llm, "get_map_reduce_strategy", None)`; when absent, it uses `SingleCallMapReduce()` directly. This is the concrete answer to "keep our existing changes for whenever Mistral is back": Mistral's code path isn't touched at all, isn't even aware this mechanism exists, and behaves identically to today whether or not this ADR ships.

`GroqProvider` implements it: returns `BatchedMapReduce(budget_chars)` when `max_request_tokens` is set (budget derived the same way as Decision 4 below), else `SingleCallMapReduce()` — consistent with ADR 015 Decision 3's "unset = disabled" philosophy: disabling the size preflight also opts out of automatic batching, since there's no longer a budget to batch against.

### 4. Wire it into `engines/llm_text.py` — gated on `reduce_prompt`, not on every `single_chunk` task

```python
strategy = SingleCallMapReduce()
if reduce_prompt:
    get_strategy = getattr(llm, "get_map_reduce_strategy", None)
    if get_strategy:
        strategy = get_strategy(config["llm"].get("max_request_tokens"))

chunks = chunk_by_paragraphs(raw_text, max_chars=strategy.effective_chunk_chars(max_chars))
...
if total > 1 and reduce_prompt:
    reduced = strategy.reduce(llm, reduce_system_prompt, parts)
```

The `if reduce_prompt` gate is deliberate and matches the finding in Context: a task without a `reduce_prompt` can't safely be chunked at all (ADR 014 Decision 1's hard-failure case), so shrinking its chunk size would only make it *more* likely to hard-fail, never less. Only the six tasks that already have a `reduce_prompt` — exactly the six genuinely large ones — ever see `effective_chunk_chars()` return something smaller than the task's own `max_chars`. The six small tasks are completely unaffected by this ADR; their one real edge case (`s4 briefing`) is a preflight-tuning question, not a map-reduce question, and stays out of scope here.

The existing `_check_request_size()` preflight (ADR 015 Decision 3) still runs afterward, over `strategy.effective_chunk_chars()`'s own output — so a mis-derived budget still fails loud before any API call, rather than silently producing an oversized request.

### 5. Deriving the budget

`BatchedMapReduce`'s character budget reuses this project's own existing token estimate rather than inventing a second one: `budget_chars = max_request_tokens * 3` (inverting `_estimate_tokens()`'s `chars / 3`), minus the task's own system prompt length, so the *chunk* plus the *system prompt* together stay under the same budget the preflight already enforces. This keeps exactly one notion of "how big is too big" in the codebase, derived from the one real data point already confirmed (ADR 015's Implementation notes: this estimate runs ~1.6× conservative against Groq's real tokenizer) — no new constant to tune independently and let drift out of sync with `max_request_tokens`.

---

## Alternatives Considered

- **Upgrade to Groq's Developer tier** (this ADR's original recommendation). Would still be the simpler fix — zero new code, since `max_chars` already bounds each chunk and a ~10× ceiling would clear the large tasks' actual bottleneck with real margin. Rejected as the plan to build around, not because the reasoning was wrong, but because it's not purchasable: Developer tier signups have been closed since early August 2026 (see Context), with no visible reopening date, and Enterprise tier isn't a fit for this project's scale. **Not dropped from the record** — if Developer tier reopens, it's a legitimate, lower-effort alternative to maintain the batched strategy long-term (`GroqProvider.get_map_reduce_strategy()` could simply return `SingleCallMapReduce()` again once the account's real ceiling comfortably clears the map step, no different from how it already falls back today when `max_request_tokens` is unset) — but building around an indefinitely-closed signup page isn't a plan.
- **A third LLM provider, used only for the six large tasks.** More plausible now that Developer tier is off the table, but still not preferred: introduces a third integration's worth of provider-specific quirks and prompt-reliability testing (per ADR 015's own experience, a new provider reliably surfaces at least one real defect on first contact) for a problem the batched strategy solves within the account already in use, reusing prompts already tested for real (ADR 014).
- **Extend ADR 014's map-reduce to the six small tasks too, so all twelve go through the same mechanism.** Rejected per the Context section's own finding: those six almost certainly don't need it (their real inputs are tiny), and applying chunking logic to a task with no `reduce_prompt` doesn't help it — it only exposes it to ADR 014's hard-failure guard for no benefit. If any of the six is later confirmed to genuinely need a `reduce_prompt` (not just a preflight tweak), that's its own small, task-specific follow-up — not something to build speculatively now.
- **Batch by a fixed draft count (e.g. every 3 drafts) instead of by size.** Rejected: a fixed count doesn't actually bound the batch's *character* size, which is what the ceiling cares about — `story-map`'s per-chunk waypoint-list drafts are exactly the case where draft size varies enough that a count-based batch could still overflow the budget while a smaller count elsewhere wastes a call. Sizing batches the same way chunks are sized (`group_by_size()`) keeps one mental model for both.
- **Do nothing until Mistral access returns or Groq's Developer tier reopens.** Rejected: this is real, current capability loss with no visible end date on either dependency — System 1D's marketing deliverables, System 4's strategic intelligence, and System 5's candidate evaluation are all unusable today.

---

## Consequences

**Easier:**
- All twelve tasks become usable again without depending on any account tier or a third provider — the six large tasks get a real, general fix; the six small tasks are already fine and stay untouched.
- The fix holds for an arbitrarily long future book, not just the 460,733-character one seen so far — batching handles growth in the number of drafts, not just their individual size.
- Zero risk to Mistral (or any future high-ceiling provider): `SingleCallMapReduce` is a pure extraction, and `MistralProvider` doesn't even need to know this mechanism exists.
- If Developer tier ever reopens, the same abstraction absorbs that too, cheaply (Alternatives Considered).

**Harder / needs care:**
- **New code, new failure surface.** `group_by_size()`'s refactor needs the same "verify with a real/synthetic input, confirm byte-identical output" discipline ADR 015 used — a subtle off-by-one in the generalization would affect every chunked task in the project (S1B's `cleanup`/`ortho`/`copyedit` included), not just the six this ADR targets.
- **Recursive reduction is a new prompt-reliability question, not just a new code path.** Every existing `reduce_prompt` (ADR 014) was written and tested assuming its input is always N *first-generation* per-chunk drafts — never previously-reduced text. A second-round reduce call feeds a reduce prompt its own prior output as input; nothing guarantees today's six reduce prompts handle that gracefully (e.g. `story-map`'s waypoint-dedup rule, tuned against raw per-chunk drafts, might behave differently against an already-partially-merged waypoint list). This needs real-call testing per task, specifically constructed to force at least two reduce rounds — not assumed safe by analogy to the single-round case ADR 014 already validated.
- **Only fires for a book long enough to need it.** The one real trigger case (`historia-expedicion-asia-vol3`, 5 chunks) never reaches a *second* reduce round at any plausible budget (5 drafts easily fit one batch) — so the recursive path this ADR cares most about protecting against needs a deliberately larger synthetic fixture to exercise for real, not the existing test book.
- **Adds a class hierarchy to a codebase that has, until now, used plain functions for every engine** (ADR 001's established shape: `run()` per engine module, no OOP). This is a deliberate, scoped exception — mirroring ADR 015's own provider hooks, not a general shift in style — worth stating plainly so it doesn't read as an unexplained inconsistency later.

---

## Implementation Checklist

**Refactor (no behavior change):**
- [ ] Add `group_by_size()` to `lib/chunker.py`; reduce `chunk_by_paragraphs()` to a one-line wrapper over it
- [ ] Verify byte-identical chunking output before/after, against a real long input (e.g. `historia-expedicion-asia-vol3`'s 460,733-character brief) and the `books/test` fixture

**Strategy classes:**
- [ ] Add `lib/map_reduce.py`: `MapReduceStrategy`, `SingleCallMapReduce` (extracted verbatim from `engines/llm_text.py`'s current reduce logic), `BatchedMapReduce` (budget-based `effective_chunk_chars()`, recursive `reduce()` via `group_by_size()`)
- [ ] Add `GroqProvider.get_map_reduce_strategy()`; confirm `MistralProvider` needs no changes at all
- [ ] Wire `engines/llm_text.py` to consult the strategy only when `reduce_prompt` is set, per Decision 4's gating

**Validation (real calls, not simulated):**
- [ ] Real-call test, all six large S1D tasks, against `historia-expedicion-asia-vol3`'s real brief — confirm the map step's per-chunk requests now clear Groq's free-tier ceiling, and the reduce step (5 drafts, one batch, one round) still produces correct output — diff against the same tasks' pre-ADR-015 Mistral-produced outputs where available
- [ ] Real-call test specifically forcing a **second** reduce round: a synthetic fixture large enough to produce enough drafts that one batch can't hold them all at the configured budget — for at least `synopsis` (simplest reduce shape) and `story-map` (the one already flagged as most at-risk for aggregate size) — check for merge artifacts across the batch boundary the same way ADR 014's own implementation notes did for the single-round case
- [ ] Confirm the six small tasks are genuinely unaffected: `s2 status`/a manual run of `s4 briefing` behaves identically before and after this change
- [ ] Confirm `_check_request_size()` still fires correctly against `strategy.effective_chunk_chars()`'s output, not just the task's original `max_chars`
- [ ] Update the README's `llm_text` engine description and `docs/adr/014-...`'s own cross-references if this changes how that ADR's mechanism is described going forward
