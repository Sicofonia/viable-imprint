# ADR 012 — System 4: Outbound Amplification (Article Briefs → Drafts)

**Status:** Proposed. Not yet implemented — for review before work starts.

---

## Context

Every automated piece of System 4 built so far (ADR 003) moves in one direction: environmental signal comes *in* (`scan`), gets synthesized *down* into something a human can digest (`briefing`). A private follow-up review of the implemented project (`~/.claude/viable-imprint-next-steps.md`) named the gap this leaves, in Beer's own vocabulary for the two things a viable system does with variety:

> **S4 amplification loop (variety amplifier) — currently attenuation-only**
> - **Outbound:** a content-strategy task in S4 that reads the latest briefing and produces short article briefs (imprint-level positioning: which signals to turn into blog posts). Drafting the articles themselves is an S1D-style `llm_text` task reading those briefs. S4 decides where to point the amplifier; S1D turns the crank — keep that split.
> - **Inbound (closes the currently open loop):** add Google Search Console as a new source in `s4 scan` ... Queries/impressions/CTR per page = the amplifier's gain measurement.

`docs/vsm.md` itself doesn't use "amplifier"/"attenuator" — that framing is the review's own, applying Beer's standard vocabulary (an attenuator reduces incoming variety to something manageable; an amplifier increases outgoing variety so the organisation can act on its environment, not just absorb it) to what's already built. `vsm.md` does list, under System 4, *"exploring new formats and channels"* and, under System 1D, *"communication and marketing for each title (social media, newsletter, specialist communities)"* — outbound content marketing is squarely inside what both systems are already meant to do; the pipeline just doesn't do any of it yet beyond the newsletter (ADR 008).

**Explicit, user-directed scope cut for this ADR: outbound only.** The review's own second half — Google Search Console (and, as a fallback, Bing Webmaster Tools) as the inbound gain-measurement source — is deliberately **not** part of this ADR. The user's stated reason: distrust of surveillance-capitalism infrastructure, Google's specifically. This isn't a technical rejection (the review's own technical case for Search Console — free API, no on-site script, no traffic-based pricing — is sound) but a values call, squarely the kind of judgment `vsm.md`'s System 5 reserves for a human, not a default this codebase should reach for just because it's free and well-documented. See "Deferred: inbound gain measurement" below — this stays a real, named gap, not quietly dropped.

---

## Decision

### 1. Two new tasks, reusing `llm_text` unchanged — same shape ADR 003 and ADR 002 already proved

- **`s4 content-strategy`** — one `llm_text` call over the latest `briefing` output. Produces *one* structured article brief per run: a working title/angle, why it's timely (grounded in, and citing, specific signals from that briefing — not invented), the intended reader, 3–5 key points to cover, suggested format/length. One brief, not a list — matching `s1d brief`'s own shape (a single document, not a menu to manage) and avoiding the need for any selection mechanism this ADR doesn't otherwise need. If a publisher wants a different angle, re-running `content-strategy` against the same briefing is cheap and immediate.
- **`s1d article-draft`** — one `llm_text` call over that one brief, drafting a full blog-post-length article (Markdown, ready for whatever the imprint's own site build already is — the review's own aside mentions a Hugo/Netlify setup). Same "extract once, then expand faithfully" discipline as `brief` → `synopsis`/etc. (ADR 002): only develop what's grounded in the brief, don't introduce new specific claims the brief didn't make.

Both are prompt files plus a `tasks.yaml` entry — zero new engine code, exactly ADR 003's own precedent for `briefing` ("a straight reuse of `engines/llm_text.py`, completely unchanged... no new code needed for this half of the pipeline at all").

### 2. No new perpetual root, no new S2 wiring — reuses `intelligence/`'s existing shape, exactly ADR 002's cross-system chaining pattern one level up

`content-strategy` is declared in `systems/s4/tasks.yaml`, invoked manually like `briefing` already is (`pipeline.py s4 content-strategy intelligence/s4/briefing/<date>/combined.txt`) — S4 has never been part of System 2's orchestration (ADR 004 point 7 excluded it deliberately), so this needs no orchestrator changes at all. Its output lands at `intelligence/s4/content-strategy/<date>/combined.txt`, same convention as `scan`/`briefing`.

`article-draft` is declared in `systems/s1d/tasks.yaml` (S1D owns drafting, matching the review's own "S4 decides where to point the amplifier; S1D turns the crank" split, and `vsm.md`'s own placement of "communication and marketing" under S1D) but marked **`book_scoped: false`** — the exact mechanism ADR 008 built for the newsletter trio, needed here for the identical reason: S1D's `tasks.yaml` *is* one of `orchestrator.BOOK_SYSTEMS`, so without the flag this task would get silently pulled into every book's `s2 run <book_slug>` graph, which makes no sense for a catalogue-level content-marketing artifact with no book behind it.

Unlike the newsletter, **no new perpetual root is needed at all.** `article-draft` is invoked manually with `content-strategy`'s output file as its input (`pipeline.py s1d article-draft intelligence/s4/content-strategy/<date>/combined.txt`) — `lib.paths.book_root()` resolves its root the same way it always does, by walking up from the given file until it finds a `manifest.yaml`. Since that file already lives under `intelligence/` (which has had its own `manifest.yaml` since ADR 003), it resolves there, and `article-draft`'s output lands at `intelligence/s1d/article-draft/<date>/combined.txt` — nested under S4's existing root, namespaced by system, exactly the same cross-system pattern ADR 002 already established for `s1d brief` reading `s1b/copyedit/es/` and writing under its own `s1d/` folder, just one level up from book-scoped to imprint-scoped. This is the whole reason `book_scoped: false` is sufficient on its own here and a dedicated root (like `newsletter/`) isn't needed: the newsletter's tasks needed their own root because nothing else already gave them one to land in; `article-draft`'s input file already does.

### 3. Both manual, on-demand — not chained into an automatic pipeline

Neither task is wired into `s2 run` or a new periodic `s2`-style command group. A publisher runs `s4 content-strategy` when they want a fresh angle, reads it, and only runs `s1d article-draft` if they actually want that angle written up — this is a real editorial choice point (not every generated brief is worth turning into a published article), and this project's standing discipline is that a human makes the call at exactly this kind of fork, not that the pipeline auto-advances through it. No new CLI group, no new orchestration code.

### 4. Deferred: inbound gain measurement — explicitly not built here, and not defaulting back to Search Console/Bing when it is

The amplification loop this ADR closes is genuinely half a loop: it gives System 4 a way to push variety outward (an article, informed by real scanned signal), but nothing measures whether that push landed — no queries, impressions, click-through, or even raw visits per published article. Beer's own point about amplifiers is that they need a matching measurement channel to know if the amplification is doing anything; without one, `content-strategy`/`article-draft` produce content on faith, not signal.

**This is left as an open TO-DO, not silently dropped.** The review's own suggestion (Google Search Console, Bing Webmaster Tools as a same-shape fallback) is rejected here on the user's explicit values grounds, not a technical one — both are free, well-documented, and would have been a small `s4 scan` source addition. **When this gap is revisited, the self-hosted alternative the review itself already named for a related, later concern — Umami or Plausible Community Edition, run on infrastructure the imprint controls rather than a third party's — is the more promising starting point specifically because it doesn't have the same surveillance-capitalism dependency**, not because it's technically superior. Worth designing as its own ADR once actually picked up: it would need the imprint to actually be running one of those (a real deployment decision, not just a config key), and a new `s4 scan` source type reading its API/export rather than Search Console's.

---

## Alternatives Considered

- **Including Search Console (or Bing) in this ADR after all, with a config-level opt-out** — rejected per the user's explicit direction: the point isn't "off by default," it's "not in this codebase's design at all" until a privacy-respecting alternative is actually chosen. An opt-out still means shipping and maintaining code for infrastructure the user doesn't want to depend on.
- **A generic "web analytics source" abstraction now, to be filled in later with whichever tool is picked** — rejected as premature: this project's own established discipline is not to build a generalized interface before a second concrete instance justifies it (see ADR 007/008's "wait for a second periodic pipeline" precedent, later actually unified in PR #9). One deferred, unspecified inbound source doesn't yet justify an abstraction; the eventual ADR can decide that once there's a real target to design against.
- **`content-strategy` producing a list of several candidate briefs instead of one** — rejected for v1: a list needs a selection mechanism (which one to draft?) this ADR doesn't otherwise need, and re-running `content-strategy` for a different angle is already cheap. Revisit only if one-brief-per-run turns out to feel limiting in practice.
- **A new perpetual `articles/` root for `article-draft`'s output, mirroring `newsletter/`** — considered and rejected (point 2): unlike the newsletter trio, `article-draft`'s natural input already lives inside a folder with its own `manifest.yaml` (`intelligence/`), so `paths.book_root()`'s existing directory-walk already resolves correctly with zero new code — introducing a dedicated root would be pure duplication of a mechanism that already works here for free.
- **Wiring `content-strategy`/`article-draft` into a new periodic `s2 amplify run` chain, mirroring homeostat/newsletter** — rejected (point 3): those two pipelines are unconditional by design because *every* run of them is wanted (a monthly newsletter, a periodic confrontation dashboard). Not every `content-strategy` brief is worth drafting — the human choice point here is real editorial judgment, not friction to engineer away.

---

## Consequences

**Easier:**
- System 4 stops being purely attenuation — there's now a mechanical path from "a scanned signal" to "a draft article grounded in it," closing (half of) the gap the review named.
- Zero new engine code, zero new perpetual root, zero new orchestration — this reuses `llm_text.py`, `intelligence/`'s existing shape, and ADR 008's `book_scoped: false` mechanism exactly as they already exist.
- A publisher gets a cheap way to turn "we noticed X in this week's briefing" into a first-draft blog post without opening a blank page.

**Harder / needs care:**
- The loop really is half-closed, on purpose (point 4) — this ADR should not be mistaken for "S4 amplification, done." A publisher (or a future session) revisiting `next-steps.md` needs to see this file, not just the checkbox, to know why the inbound half is still open and what direction was already chosen for it.
- `article-draft`'s output landing inside `intelligence/` (an S4-named root) rather than somewhere S1D-named is a little surprising on first encounter, even though it's mechanically identical to how `s1d brief` already writes into a *book's* folder rather than its own — worth the explicit callout in point 2 so a future contributor doesn't "fix" it into a new root that isn't actually needed.
- Article quality is only as good as the underlying `briefing` — a thin or stale scan (ADR 003's own named risk: "a stale or narrow watchlist quietly produces stale or narrow intelligence with no error to signal it") now has a second downstream consumer that inherits the same silent risk.

---

## Implementation Checklist

- [ ] Add `content-strategy` to `systems/s4/tasks.yaml` (`engine: llm_text`)
- [ ] Write `prompts/s4/content_strategy_task.txt` (real, Spanish) and `prompts/examples/s4/content_strategy_task.txt` (English reference) — one grounded article brief per run, citing which briefing signal(s) it responds to, no invented claims
- [ ] Add `article-draft` to `systems/s1d/tasks.yaml` (`engine: llm_text`, `book_scoped: false`)
- [ ] Write `prompts/s1d/article_draft_task.txt` (real, Spanish) and `prompts/examples/s1d/article_draft_task.txt` (English reference) — full Markdown article from one brief, extract-then-expand discipline (only develop what the brief grounds)
- [ ] End-to-end test: run `s4 content-strategy` against a real (or realistic scratch) `briefing` output, confirm the brief cites real signals from it; run `s1d article-draft` against that brief, confirm the draft stays grounded and doesn't introduce ungrounded specific claims; confirm `article-draft`'s output correctly lands under `intelligence/s1d/article-draft/<date>/` via the existing `book_root()` walk, with no new root created; confirm `s2 status`/`s2 run` on a real book's graph is unaffected (article-draft correctly excluded via `book_scoped: false`)
- [ ] Update README (System 4 section, System 1D section, command reference table, and an explicit note in whichever section tracks open work that inbound gain measurement is a deliberate, named TO-DO — not forgotten, not silently rejected)
