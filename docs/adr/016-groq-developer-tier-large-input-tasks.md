# ADR 016 — Groq Developer Tier to Unblock the Large-Input Tasks (S1D Expansion, S4, S5)

**Status:** Proposed.

---

## Context

ADR 015 made Groq (`openai/gpt-oss-120b`) the default LLM provider and validated it end-to-end for System 1B (`cleanup`, `ortho`, `copyedit`), but explicitly documented that twelve `single_chunk` tasks (ADR 014) — six in System 1D, two in System 4, two in System 5, plus `newsletter`/`article-draft` — cannot run on Groq's free tier, because their requests exceed its confirmed 8,000-token-per-minute ceiling for this model. ADR 015 named three possible remedies and deferred all of them: point those tasks back at Mistral, move them to a different provider, or upgrade to a paid Groq tier.

**Mistral is not currently available as a fallback provider.** That closes the first remedy outright — there is currently no working LLM provider for any of the twelve tasks. This ADR is about which of the remaining two remedies to take, and it turns out the twelve tasks are not equally affected, which changes the shape of the fix.

### Not all twelve tasks are equally broken — checked against real data, not assumed

**Six tasks read `s1d brief`, whose size scales with the whole manuscript:** `synopsis`, `story-map`, `one-pager`, `press-dossier`, `trailer-storyboard`, `goodreads-profile`. The one full-length book processed so far, `historia-expedicion-asia-vol3`, produced a **460,733-character** brief — the exact case ADR 014 was written to handle. At this task family's own `max_chars: 100000`, that chunks into 5 pieces (ADR 014's own confirmed count), each landing at roughly **19,000–31,000 tokens** depending on whose count is trusted: this project's own conservative `~3 chars/token` preflight estimate, or Groq's real tokenizer, extrapolated from the one confirmed real ratio ADR 015's Implementation notes captured (~4.7 chars/token, from a real 413 response). Either way, that's well over Groq's free-tier ceiling for a single request. All six already have ADR 014's map-reduce built (a `reduce_prompt` per task), so it's specifically the **map step** (each chunk, one request per chunk) that's blocked. The **reduce step** — one call reconciling the per-chunk drafts — is small for this particular book (5 drafts), but not necessarily always: `story-map`'s own per-chunk output is a waypoint list, which scales with narrative detail, not just chunk count, so a longer or more event-dense future book could plausibly push even the reduce call closer to a tight ceiling. These six are genuinely, structurally incompatible with Groq's free tier for any book of meaningful length.

**The other six read a small, bounded document, not a manuscript:** `newsletter` (a month's production digest), `article-draft` (one content-strategy brief), `s4 briefing` (one scan digest), `s4 content-strategy` (one briefing), `s5 evaluate` (one candidate description), `s5 homeostat` (one S3/S4/decision-log confrontation). Checked directly against every real instance this project has produced so far (`intelligence/`, `candidates/`, `homeostat/`, `newsletter/`):

| Task's real input | Real sizes observed | System prompt size | Estimated tokens (this project's `~3 chars/token`) |
|---|---|---|---|
| `s4 briefing`'s `scan` digest | 522 – 13,655 chars (5 samples) | 3,000 chars | up to **~5,550** (largest sample, 2026-07-26) |
| `s4 content-strategy`'s briefing | 3,955 – 6,100 chars | 3,000 chars | up to ~3,050 |
| `s5 evaluate`'s candidate brief | 676 – 3,895 chars (4 samples) | 6,786 chars | up to ~3,560 |
| `s5 homeostat`'s confrontation doc | ~1,060 – 1,173 chars | 3,231 chars | ~1,470 |
| `newsletter`'s scan digest | 3,692 – 3,764 chars | 3,934 chars | ~2,570 |
| `article-draft`'s content-strategy brief | 1,407 – 2,015 chars | 1,843 chars | ~1,290 |

None of these has ever come remotely close to the defensive `max_chars: 100000` ceiling those tasks were given, and every real sample comfortably clears Groq's confirmed 8,000 TPM free-tier limit too — **with one exception: this project's own `llm.max_request_tokens` preflight** (ADR 015, currently `4000`, deliberately conservative to cover this project's estimate running high against Groq's real tokenizer). The largest real `s4 briefing` input on record (13,655 characters, 2026-07-26) estimates to ~5,550 tokens by this project's own heuristic — over the current `4000` threshold, and would very likely have been rejected by the *local* preflight as a false positive, not by Groq itself. The other five smaller tasks stay comfortably under `4000` for every real sample seen. In short: five of the six small tasks are very likely already fine as configured; `s4 briefing` specifically needs either a higher `max_request_tokens` or confirmation it stays under it in practice during a busy scan period.

---

## Decision

### 1. Upgrade the Groq account to Developer tier

A billing/account action, not a code change: add a payment method at `console.groq.com/settings/billing`. Per Groq's own documentation and secondary sources (not yet confirmed against this account's real post-upgrade limits — see Implementation Checklist), Developer tier raises rate limits roughly **10×** over free tier for this kind of model, plus a reported **~25%** per-token discount. Even reading that conservatively, an inferred ~80,000 TPM ceiling comfortably clears the large tasks' actual bottleneck: the map step's per-chunk size for the largest real book processed so far (~19,000–31,000 tokens, per the two token counts above) is at most ~39% of that inferred ceiling, with room to spare for the reduce step too.

This is the right primary fix for three reasons specific to this project's own established preferences:

- **Zero new engineering, zero new prompt-reliability risk.** ADR 015's own hardening ("prompt reliability does not transfer with the provider... expect a full round" and ADR 014's map-reduce prompts, already tested once for real against this exact book) don't need touching. Nothing about chunking, the reduce mechanism, or any prompt changes.
- **The fix scales correctly as manuscripts grow.** `max_chars: 100000` caps every individual chunk's size regardless of total book length — a longer book produces *more* chunks, not *bigger* ones. So the map step's per-request size has a hard ceiling today (~100,000 characters, ~19,000–31,000 tokens) that a tier upgrade clears with real margin, for any book length, not just the one seen so far.
- **The cost is genuinely small and transparent.** At Groq's published $0.15/$0.60 per-million-token rate for this model (recorded as this project's own shadow-cost pricing since ADR 015), even the ~600,000-token full-manuscript pass ADR 015 estimated for a long book's System 1B stage costs under $0.40. The twelve tasks this ADR is about run far less often and on far smaller inputs than that.

### 2. Raise `llm.max_request_tokens` to match the new ceiling

Once the account is upgraded and the real Developer-tier limit is confirmed (Implementation Checklist), `config.yaml`/`config.example.yaml`'s `max_request_tokens: 4000` needs raising to a comparable fraction of the new ceiling (following ADR 015's own reasoning: leave a margin for this project's `~3 chars/token` estimate running conservative against Groq's real tokenizer, and for a multi-chunk task's later chunks competing with earlier chunks' completions inside the same rolling minute). Left at `4000` after an upgrade, this local preflight becomes the new, *wrong* bottleneck — rejecting requests Groq's real, now-much-larger ceiling would have accepted, defeating the point of paying for the upgrade.

### 3. Verify, don't fix, the six small-input tasks

Per the table above, five of the six are very likely already fine on the current (or upgraded) tier; `s4 briefing` is the one real edge case on record. Rather than change anything about these six now, confirm them with a real call each once the account is upgraded (Implementation Checklist) — this project's own established discipline (ADR 002/006/010/013/014's repeated lesson) is to check a real call before trusting an estimate, not the other way round.

---

## Alternatives Considered

- **Extend ADR 014's map-reduce to the six tasks that don't have it yet (`newsletter`, `article-draft`, `s4 briefing`, `s4 content-strategy`, `s5 evaluate`, `s5 homeostat`), and lower `max_chars` across all twelve so every chunk fits under even the free tier.** Rejected as the *primary* fix: per the table above, five of those six almost certainly don't need it at all (their real inputs are tiny), so this would be real, nontrivial prompt-authoring-and-testing work (ADR 014's own established cost per task) spent mostly on tasks that were never actually broken. For the six tasks that genuinely are large (the S1D expansion set), lowering `max_chars` shrinks each map-step chunk but produces *more* chunks — which grows the reduce step's input instead, a tradeoff ADR 014 never had to solve because it was building map-reduce for the first time, not fitting it under a hard per-minute ceiling. This doesn't fully solve the problem without further work (a batched/hierarchical reduce for very long books) and is more engineering than the actual gap justifies today. Worth revisiting as a lower-priority, cost-reducing complementary improvement if Groq's paid-tier cost or throughput ever becomes a real constraint — unlikely at this project's real call volume.
- **Per-task or per-book provider selection** (ADR 015's own deferred alternative, revisited here since it would also solve this). Still not necessary now that a single account upgrade solves the immediate problem uniformly across all twelve tasks. Remains a reasonable future architecture improvement on its own merits — e.g. if Mistral access is restored and mixing providers becomes useful again — but out of scope here, for the same reasons ADR 015 gave (touches `task_loader`, the manifest schema, and `metrics.enrich()`'s provider assumption).
- **A third LLM provider, used only for the large-input tasks.** Not pursued: introduces a third integration's worth of provider-specific quirks and prompt-reliability testing (per ADR 015's own experience: a new provider reliably surfaces at least one real defect on first contact) to solve a problem a tier upgrade already solves cleanly, with the mechanism already built and tested.
- **Do nothing — accept that the twelve tasks stay broken until Mistral access returns.** Rejected: this is real, current capability loss, not a hypothetical — System 1D's marketing deliverables, System 4's strategic intelligence, and System 5's candidate evaluation are all unusable today, and there is no fallback provider to wait this out with.

---

## Consequences

**Easier:**
- All twelve tasks become usable again, with no code change and no new prompt-reliability risk — the map-reduce mechanism and every prompt involved already exist and were already tested once for real (ADR 014, ADR 015).
- The fix holds up as manuscripts grow, since `max_chars` bounds each chunk's size independent of total book length.

**Harder / needs care:**
- **Introduces a real, ongoing (if small) cost where free-tier usage had none.** Worth being explicit about the difference from what happened with Mistral: this is a deliberate, transparent, predictable price for a specific known ceiling, not a free tier silently disappearing — but it's still a recurring dependency-on-spend this project didn't have before ADR 015.
- **`llm.max_request_tokens` must be re-tuned after the upgrade**, or it silently becomes the new bottleneck instead of Groq's own real (much higher) ceiling — easy to forget, since the symptom (a request rejected before any API call) looks identical to today's correctly-working preflight.
- **The reduce-step aggregate-size risk for `story-map` (and potentially others) on a longer or more event-dense future book is not fully eliminated, only given much more margin.** Worth a targeted look if a future book's brief is drastically larger than the 460,733-character one seen so far.
- **`s4 briefing` needs a real check**, not just an assumption, before trusting it against even the current tier — the one real case on record (13,655 characters) sits right at the edge of what this project's own conservative local estimate would allow.

---

## Implementation Checklist

- [ ] Upgrade the Groq account to Developer tier (account/billing action, not a code change)
- [ ] Confirm real Developer-tier limits for `openai/gpt-oss-120b` at `console.groq.com/settings/limits`, correcting this ADR's inferred ~10×/~80,000 TPM figure if it differs
- [ ] Raise `llm.max_request_tokens` in `config.yaml`/`config.example.yaml` to a margin under the confirmed new ceiling, following ADR 015's own reasoning for the margin
- [ ] Real-call validation: one full System 1D expansion pass (all six brief-reading tasks) against `historia-expedicion-asia-vol3`'s real 460,733-character brief (or a fresh, comparably-sized fixture) — confirm both the map step and the reduce step succeed end to end for every task, not just the ones ADR 014 tested against Mistral originally
- [ ] Real-call validation: `s4 briefing` against a real scan digest at or near its largest historical size (13,655 characters), confirming it clears both the (raised) local preflight and Groq's real ceiling
- [ ] Spot-check the remaining small tasks (`s4 content-strategy`, `newsletter`, `article-draft`, `s5 evaluate`, `s5 homeostat`) with one real call each, confirming no regression
- [ ] Update `config.example.yaml`'s comments and the README to note the account now runs on Groq's Developer tier and why, so a future reader doesn't assume free-tier limits still apply
