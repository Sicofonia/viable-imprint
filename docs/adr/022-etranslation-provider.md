# ADR 022 — eTranslation as the Default Translation Provider

**Status:** Implemented (2026-10-08). Receiver deployed and verified; provider verified with real calls on a chapter-sized input. Not yet verified: resume after a real interruption, a full-length book, the `html` format, and the LLM-enhanced domains. See **Implementation Notes**.

---

## Context

DeepL has changed its free-API terms, and the 500,000-characters-a-month allowance this project's
production plan was built around is no longer available to this account. That allowance already
shaped real work: the first production book (`historia-expedicion-asia-vol3-part1`) had to be
split across two monthly quota windows. Without it, `s1b translate` has no affordable path for a
full manuscript.

The account owner has registered with the European Commission's **eTranslation** service (DGT's
neural machine translation, open to EU SMEs at no charge) and wants it to become the pipeline's
default translation provider. DeepL stays available as a selectable alternative.

### What eTranslation's API actually is

Confirmed against the Commission's own published material, not secondary sources: the REST v2
OpenAPI spec (`language-tools.ec.europa.eu/assets/downloads/eTranslation-RestV2.yaml`) and the
"Dev Corner" pages it belongs to (Guidelines, Authentication, Rate Limits, Efficient API
Consumption).

- **One endpoint:** `POST https://language-tools.ec.europa.eu/etranslation/api/askTranslate`,
  HTTP **Basic** auth (`application name : password`, issued at registration). REST v1 and SOAP
  (Digest auth, `webgate.ec.europa.eu/etranslation/si/...`) still exist; v2 is the one the
  Commission recommends.
- **Two request shapes, mutually exclusive:** `textToTranslate` (a snippet, **hard limit 5,000
  characters**) or `documentToTranslate` (a file, inline as base64 or pulled from a URL/FTP/SFTP,
  **hard limit 20 MB of base64**). Accepted formats include `txt`, `html`, `xml`, `docx`, `odt`.
- **Fully asynchronous, with no way to fetch a result.** The submit call returns only a numeric
  `requestId` (or a negative error code). The translation is *pushed* later to a destination named
  in the request: an HTTP(S) callback, FTP, or SFTP. The Commission's own words: "There is no
  endpoint that blocks and returns the translation in the same HTTP response." For documents, both
  `deliveries` (where the file goes) and `notifications` (success/failure pings) are required.
  REST v1's e-mail destination does not exist in v2.
- **HTTP delivery is a JSON POST** of `{requestId, sourceLanguage, targetLanguage,
  externalReference, outputFormat, result}`, where `result` is the translated file as base64. It
  is retried up to 3 times on a `5xx`, and "in rare cases" delivered more than once, so the
  receiver must be idempotent (dedup key: `requestId` + `targetLanguage`). Callbacks arrive with
  a fixed `User-Agent: etranslation/2.0`.
- **The Commission explicitly recommends one document request over many snippets:** "Don't split
  a long document into multiple snippet calls just to stay under the 5,000-character limit. Use
  `documentToTranslate` instead."
- **Rate limits:** a concurrency quota of 50 active documents and 50 active snippets per client.
  A `QUOTA_EXCEEDED` (`-11200`) error exists, but no volume quota is published.
- **Useful extras, not used by this ADR (see "What does NOT ship"):** a per-request `glossary`
  (two-column xlsx/tsv); an optional `llm` block for LLM-enhanced translation; a
  `[notranslate]...[/notranslate]` marker; a `GET /status` endpoint reporting system load
  (`0` normal … `3` blocked).
- **One undocumented loose end:** the published error-code list includes
  `PICKUP_AND_DELIVERIES_ARE_MUTUALLY_EXCLUSIVE` (`-20001`) and `CANNOT_DELIVER_BY_PICKUP`
  (`-70010`), implying a "pickup" (client-pulls) delivery mode that the v2 spec does not
  describe. If it exists and is open to this account, it would remove the need for Decision 2
  entirely. It is not designed around here; see Alternatives.

### The structural problem this creates

Every provider this project has used so far — DeepL, Mistral, Google AI Studio, Z.ai — answers
in the same HTTP response. `s1b translate` is a local CLI command run on the account owner's own
machine; it has no public address and nothing listening for an incoming POST. eTranslation
cannot be used at all, for snippets or documents, without *something* public to receive the
result. That receiver is the real design question of this ADR; the provider class itself is
straightforward.

---

## Decision

### 1. Send the whole manuscript as one document request

`documentToTranslate` with an inline base64 file — the same "whole document, no chunking" shape
`s1b translate --document` already gives DeepL, and the shape the Commission itself recommends.
A full production manuscript (~450K characters) is roughly 2–3% of the 20 MB limit.

The submitted document is **HTML, not plain text**, built by the provider from the cleaned text
(the same "convert our markup to something the service understands, then convert back" pattern
`DeepLProvider._to_xml()` / `_from_xml()` already uses):

| Cleaned text | Sent as | Why |
|---|---|---|
| blank-line-separated block | `<p>...</p>` | Paragraph boundaries are what `ortho`, `copyedit` and `format` rely on downstream. |
| single newline inside a block | `<br/>` | `lib/odt_writer.py`'s heading detection depends on a chapter number and title sharing one block on separate lines. |
| `[i]...[/i]` | `<i>...</i>` | eTranslation preserves inline formatting for HTML; it has no `tag_handling` equivalent for bracket text. |
| `[sc]...[/sc]` | `<span class="sc">...</span>` | Same reason. |
| `[FN: ... [/FN]]` | left as literal text | The closing-marker variants (`]` vs `[/FN]]`) make a reliable tag conversion riskier than the problem it solves. DeepL's document mode carried these through intact; whether eTranslation does is the first thing the tracer bullet checks (Implementation Checklist). |
| `&`, `<`, `>` | escaped | Valid HTML. |

`outputFormat` is left unset, so the result comes back as HTML and is converted back to this
project's bracket markup before being written. Plain `.txt` (the Commission's "most efficient"
format) is the fallback if HTML round-tripping proves unreliable, selectable by config
(Decision 5) without code changes. Which one actually preserves markup better is an empirical
question, not assumed here.

### 2. A small receiver on Vercel, outside the pipeline

The account owner's choice, over Netlify (whose free plan's hard credit cap is shared with the
imprint's own website — exhausting it would pause the site) and the other options in
Alternatives.

A new, self-contained directory `receivers/etranslation-vercel/`, deployed by hand with the
Vercel CLI, never imported or run by the pipeline:

- **One Vercel Function** (`api/etranslation.js`, Node, using the official `@vercel/blob` SDK):
  - `POST ?kind=delivery|success|failure&token=…` — called by eTranslation. Stores the JSON body
    in Vercel Blob at `etranslation/<requestId>/<kind>.json` (overwrite = idempotent, which also
    handles the duplicate-delivery case). Returns `200`.
  - `GET ?requestId=…&token=…` — called by the CLI. Returns `{"status": "pending"}`, the stored
    delivery, or the stored failure.
  - `DELETE ?requestId=…&token=…` — called by the CLI after it has written the result to disk.
- **Authentication is a shared secret in the query string**, compared in constant time against
  the function's `RECEIVER_SECRET` environment variable. eTranslation offers no way to attach an
  auth header to its callbacks, so the URL itself must carry the secret; it is single-purpose and
  rotatable by redeploying with a new value. `User-Agent: etranslation/2.0` is checked on POSTs as
  a cheap filter, not as security.
- **Storage hygiene:** results are deleted as soon as the CLI has them, so the store holds at most
  the in-flight books. The blob store should be private if Vercel offers that for the account;
  otherwise blobs get random-suffixed paths and are reachable only through the function, never
  listed or linked.
- **Why Node, in a Python project:** `@vercel/blob` is a JavaScript SDK. A Python function would
  have to hand-roll calls to Blob's REST API with no official client. A ~80-line JS file in its
  own directory, with its own `package.json`, is the smaller risk. It is a deliberate, contained
  second language, named here so it isn't mistaken for drift.

**Limits that bound this, all comfortably met:** the Vercel Function request body limit is a hard
4.5 MB on every plan. A translated 450K-character book is roughly 0.6 MB as text and under 1 MB as
base64. Decision 3's per-request character ceiling keeps any single delivery under that limit by
construction.

**Licensing check before deploying — the account owner's action, not the pipeline's:** Vercel's
Hobby (free) plan is, as understood at the time of writing, restricted to personal,
non-commercial use, and Ecos de Oriente is a commercial imprint. Confirm against Vercel's current
fair-use terms before relying on Hobby. Decision 4 keeps the CLI independent of Vercel, so moving
the receiver later (Netlify, a Pro plan, a self-hosted box) changes a URL in config, not code.

### 3. `providers/translation/etranslation.py` — `ETranslationProvider(TranslationProvider)`

`translate(text, source_lang, target_lang)` performs the whole asynchronous round trip and only
returns once the translation is back (or fails), so the rest of the pipeline sees the same
synchronous `text in, text out` contract it has today:

1. `GET /status` — fail fast at level `3` (blocked), warn at level `2` (critical load).
2. Convert to HTML (Decision 1); `POST /askTranslate` with `documentToTranslate.document`,
   `sourceLanguage`/`targetLanguages`, `domain`, an explicit `llm` block (Decision 5),
   `deliveries.http` and `notifications.success/failure.http` all pointing at the receiver with
   the secret, and `callerInformation.externalReference` set to `<book-slug>/<input-stem>` for
   human-readable correlation.
3. Record the `requestId` (Decision 6), then poll the receiver every `poll_interval_seconds` until
   a delivery or failure arrives, or `timeout_minutes` elapses.
4. On delivery: decode base64, convert HTML back to bracket markup, `DELETE` from the receiver,
   return the text. On failure: raise a `ClickException` naming eTranslation's `errorCode` and
   `errorMessage`.

A negative error code on submit is fatal and named (`-11200` gets an explicit "quota exceeded"
message; the `-20000`-range concurrency error gets "50 documents already in flight"). Transient
network errors on submit and on polling are retried with backoff, the same posture as the LLM
providers' loops. Polling a receiver that returns `pending` is not an error and never counts
against any retry budget.

`translate_document()` is **not** implemented. `translate()` already sends the whole input as one
document, so `--document` would add nothing; the existing `getattr()` check in
`engines/translation.py` fails clearly if it's passed.

`usage` is `{"characters": <len of source text submitted>}`, counted locally. eTranslation reports
no billed-character figure, so the number is labeled as a local count in the provider's
docstring, not presented as the service's own.

### 4. Make chunk size a provider attribute instead of an engine constant

`engines/translation.py` hardcodes `chunk_by_paragraphs(raw_text, max_chars=50000)`, with a comment
that is really a fact about DeepL's request limit. Moved to where it belongs:

- `DeepLProvider.max_chars_per_request = 50000` (comment moves with it; behavior unchanged).
- `ETranslationProvider.max_chars_per_request = 2_000_000` — not eTranslation's limit (20 MB is far
  higher) but the receiver's: 2M characters × Spanish expansion × UTF-8 accents × HTML markup ×
  base64's 4/3 stays under Vercel's 4.5 MB body limit with margin. Every real manuscript is one
  chunk, so in practice this is the whole-document request of Decision 1; anything larger is
  split by the existing `chunk_by_paragraphs()` into several document requests instead of
  failing at delivery time.
- The engine reads `getattr(translator, "max_chars_per_request", 50000)`. The fallback keeps any
  future provider that doesn't declare one on today's behavior.

This applies ADR 018's lesson to translation: a size limit belongs to the provider that has it,
so switching `translation.provider` changes the chunk size automatically instead of leaving a
number validated against one provider in front of another.

### 5. Configuration: per-provider sub-blocks, with pricing moving into them

```yaml
translation:
  provider: etranslation          # was: deepl
  source_lang: EN
  target_lang: ES
  etranslation:
    receiver_url: https://<your-project>.vercel.app/api/etranslation
    domain: GEN                   # "General text"; see GET /getDomains for the account's list
    document_format: html         # html | txt — see Decision 1
    llm_enhanced: false           # sent explicitly, not left to the account default
    poll_interval_seconds: 30     # guess; revisit after real turnaround times are seen
    timeout_minutes: 120          # guess; same
    pricing:
      per_million_characters: 0.0 # free for eligible SMEs; a real figure, not a shadow one
  deepl:
    pricing:
      per_million_characters: 20.00
```

Secrets go in `.env`, matching every existing key: `ETRANSLATION_APP_NAME`,
`ETRANSLATION_PASSWORD`, `ETRANSLATION_RECEIVER_SECRET` (the same value set as `RECEIVER_SECRET`
on Vercel). `get_translation_provider()` gains an `etranslation` branch that checks all three plus
`receiver_url`, and fails with a message naming whichever is missing.

**Pricing moves under each provider.** Today `translation.pricing` is a single top-level block,
which is provider-blind: switch to eTranslation and the S3 dashboard would keep charging DeepL's
rate for free translations. `lib/metrics.py`'s `_translation_cost()` reads
`translation.<provider>.pricing`. For backward compatibility, a legacy top-level
`translation.pricing` is still honored **only when the provider is `deepl`**, since that's the only
provider it was ever written for, and ignored otherwise.

`llm_enhanced: false` is explicit for reproducibility: if the account default ever changes
server-side, output shouldn't silently change with it. Whether LLM enhancement produces better
literary Spanish than the plain NMT engine is worth a real comparison (Implementation Checklist),
not a default chosen blind.

### 6. Resume an in-flight request instead of resubmitting it

A full-book translation may take long enough that the CLI is interrupted (Ctrl-C, a closed
laptop, a tool timeout). This has already happened in this project's own testing (ADR 021's
2026-09-30 notes). Resubmitting would translate the book twice and consume concurrency quota for
nothing.

The provider writes a small sidecar next to the output, `s1b/translated/<lang>/.<input-stem>.etranslation.json`,
holding each chunk's `requestId`, submission time, and a SHA-256 of the chunk's source text, before
it starts polling. On the next run of the same task:

- sidecar present and hash matches → **resume polling** that `requestId`, no new submission;
- hash differs (the cleaned text changed) → the old request is stale, submit fresh;
- task completes → sidecar deleted.

It is a plain file, not a `manifest.yaml` field: it's transport bookkeeping for one provider, not
run-state, and the ledger's single-writer rule (ADR 004/009) stays untouched. It also means
`s2 run` gets resume for free: a timed-out `translate` is recorded `failed`, and the next `s2 run`
retries it, which resumes instead of resubmitting.

### 7. Documentation

README (Requirements, the `providers/` paragraph, the translate section, `.env` setup), `docs/cheat_sheet.txt`,
`.env.example`, `config.example.yaml`, and a `receivers/etranslation-vercel/README.md` with the
one-time deploy steps (create a Blob store, set `RECEIVER_SECRET`, `vercel deploy`, paste the URL
into `config.yaml`).

---

## What does NOT ship in this ADR — explicit, not silent

- **No glossary support.** eTranslation accepts a per-request glossary, which is a real fit for
  this imprint (explorers' names, place names, recurring terms). It deserves its own design:
  where the glossary lives, per book or imprint-wide, and who maintains it. Note that eTranslation
  rejects a glossary combined with LLM enhancement (`-10419`), which ties that design to Decision
  5's `llm_enhanced` choice.
- **No snippet mode.** Every request is a document request, including tiny test inputs. Snippets
  are also asynchronous (the translated text arrives through the success notification), so they
  would need the same receiver for no gain.
- **No multi-target-language requests.** eTranslation can translate into several languages in one
  request; this pipeline has one `target_lang`.
- **No automated receiver deployment** from the pipeline. Deploying is a one-time manual step,
  the same posture as the account owner's Canva and POD-portal work.
- **No change to DeepL's behavior** apart from Decision 4 (where its chunk size is declared) and
  Decision 5 (where its pricing is read). Both are refactors with identical results.

---

## Alternatives Considered

- **Snippet requests in 5,000-character chunks** (~100 requests per book). Rejected: the
  Commission's guidance says explicitly not to do this, it would burn the concurrency quota, and
  it doesn't avoid the receiver anyway, since snippets are delivered asynchronously too.
- **Netlify Function + Netlify Blobs.** Technically equivalent and free, with no commercial-use
  question. Not chosen: Netlify's free plan is a hard monthly credit cap shared with the imprint's
  website, and running out pauses every site on the account. Remains the obvious fallback if
  Vercel's Hobby terms rule it out, and Decision 2's URL-only coupling makes the switch cheap.
- **SFTP delivery to a server the account owner controls.** Clean, and eTranslation supports it
  natively, but there is no such server today, and running one is more upkeep than a function.
- **A temporary tunnel to the local machine** (ngrok, Cloudflare Tunnel). Rejected: a third-party
  dependency in the critical path of every run, and the CLI's machine would have to stay up and
  reachable for the whole translation.
- **Manual: upload through eTranslation's web interface, drop the result into
  `s1b/translated/es/`.** Zero code, same posture as ADR 011's sales CSVs. Kept as the
  always-available fallback if the receiver is ever down, but rejected as the design: it breaks
  `s2 run`'s end-to-end flow and leaves markup conversion to the account owner.
- **REST v1 or SOAP with e-mail delivery.** Rejected: legacy APIs (Digest auth), and e-mail still
  needs a human or a mailbox integration to get the file back into the pipeline.
- **"Pickup" delivery mode.** Not rejected, simply unknown: it appears in the error-code list but
  not in the spec. Worth one e-mail to DGT-AI-Language-Services-Advisory@ec.europa.eu. If it turns
  out to be a supported pull mechanism, a follow-up ADR can replace Decision 2's receiver with
  polling eTranslation directly and delete `receivers/` entirely. Decisions 1, 3–6 would carry
  over unchanged.
- **Plain `.txt` as the default document format.** The Commission calls it the most efficient
  format, and DeepL's document mode carried bracket markup through `.txt` intact. Not the
  default because nothing guarantees eTranslation's engine treats `[i]` the same way, while HTML
  inline tags are exactly what its converter is built to preserve. Kept selectable (Decision 5)
  so the tracer bullet can compare both.

---

## Consequences

**Easier:**
- Full manuscripts translate in one request with no monthly character ceiling to plan books
  around. No more splitting a book across quota windows.
- Switching `translation.provider` back to `deepl` restores DeepL's chunk size and pricing
  automatically (Decisions 4 and 5).
- Interrupted translations resume instead of being paid for, and waited for, twice (Decision 6).

**Harder / needs care:**
- **The pipeline now depends on a deployed service.** If the Vercel function is down,
  misconfigured, or its secret is out of sync with `.env`, `translate` fails. The CLI should say
  which (unreachable, `401`, still `pending` at timeout) rather than just "timed out".
- **A second language in the repo** (Decision 2), contained to one directory.
- **Turnaround time is unknown.** DeepL answered in seconds to minutes; eTranslation queues work,
  and `/status` level `2` explicitly means "expect delays". `s1b translate` may become the
  longest-running step in the pipeline. Both polling defaults are guesses until a real book runs.
- **Translation quality is unknown for this material.** eTranslation's engines are trained mostly
  on EU institutional text, and this imprint publishes 19th-century travel prose. A real side-by-
  side against DeepL's existing output is part of validation, not an afterthought. `ortho` and
  `copyedit` downstream may carry more of the load than they did under DeepL.
- **Vercel's Hobby terms are an open question** the account owner must close before production
  use (Decision 2).

---

## Implementation Checklist

**Before writing code:**

- [x] Confirm Vercel Hobby's commercial-use terms (or choose a plan/host that fits)
- [ ] Optional: ask the DGT advisory team whether "pickup" delivery exists for REST v2 and this
      account (see Alternatives)
- [x] `GET /getDomains` with the account's credentials: confirm `EN-ES` is available on `GEN`, and
      whether an `LLM`-suffixed pair is listed

**Receiver (`receivers/etranslation-vercel/`):**

- [x] `api/etranslation.js`: POST (delivery/success/failure), GET (status/result), DELETE; secret
      check in constant time; User-Agent filter on POSTs; idempotent overwrite storage
- [x] `package.json`, `README.md` with deploy steps
- [x] Deploy; confirm with `curl` that a POST without the secret gets `401` and a POST/GET/DELETE
      round trip with it works

**Pipeline:**

- [x] `providers/translation/etranslation.py`: status check, HTML conversion both ways, submit,
      poll, delete, error-code messages, `usage`, sidecar resume (Decision 6)
- [x] `max_chars_per_request` on both providers; `engines/translation.py` reads it (Decision 4)
- [x] `get_translation_provider()` `etranslation` branch with key/URL checks
- [x] `lib/metrics.py`: per-provider pricing with the DeepL-only legacy fallback (Decision 5)
- [x] `config.example.yaml`, `.env.example`, README, cheat sheet
- [x] Stubbed-`httpx` unit tests (stdlib `unittest`, same pattern as `tests/test_openai_chat_zai.py`):
      HTML round trip of every markup case in Decision 1's table; submit payload shape; polling
      through `pending` → delivery and → failure; error-code messages; sidecar resume / stale-hash
      resubmit; per-provider pricing including the legacy fallback

**Real-call validation (the standard every provider so far has met):**

- [x] Tracer bullet on the `books/test` fixture: a short excerpt containing `[i]`, `[sc]`,
      `[FN: ...[/FN]]`, a chapter heading block, and an `&` — check every marker survives, with
      `document_format: html`, then with `txt` — **done for `txt` only**: no `&`, and `html` was
      not exercised against the real service (see Implementation Notes)
- [x] Record real turnaround time; adjust `poll_interval_seconds` / `timeout_minutes`
- [ ] Interrupt a run mid-poll and confirm the next run resumes the same `requestId`
- [ ] Quality side by side: the same chapter through eTranslation (`llm_enhanced: false`, then
      `true`) against DeepL's existing output in `books/test`, judged by the account owner
- [x] Confirm the S3 dashboard shows `provider: etranslation` and `cost_usd: 0.0`

---

## Implementation Notes (2026-10-08)

The Decisions above are kept as designed. What changed, what was found, and what was and was
not verified:

**Changes to the design, all small:**

- **Vercel Pro, not Hobby.** The licensing question in Decision 2 was read against Vercel's
  fair-use guidelines, which restrict Hobby to non-commercial personal use ("financial gain of
  anyone involved in any part of the production"). The account owner moved the team to Pro.
- **A private Blob store was confirmed available**, so the "private if the account offers it"
  hedge became a plain requirement. The function authenticates through OIDC once the store is
  connected to the project; no Blob token exists anywhere in this design. Private stores need
  `@vercel/blob` >= 2.3 and Vercel CLI >= 50.20.
- **The receiver logs an unexpected `User-Agent` instead of rejecting it** (Decision 2 said
  "checked on POSTs as a cheap filter"). Rejecting on a header would turn any change on
  eTranslation's side into a silent loss of every translation, for no security gain: the shared
  secret is the actual gate.
- **The sidecar also stores each received translation, not just request ids** (Decision 6).
  Without that, a crash between deleting a result from the receiver and writing the output file
  would lose a finished chunk with no way to recover it. A matching entry holding a translation is
  returned with no network call at all.
- **`llm_enhanced` is true / false / null**, not just a boolean (Decision 5). `null` omits the
  `llm` block entirely, for an account that gets `LLM_NOT_ALLOWED` even when `enabled` is false.
  Whether that can happen is unknown; the cost of allowing for it is one config value.
- **Two optional hooks, `begin_run()` and `finish_run()`,** carry the sidecar's lifecycle between
  `engines/translation.py` and the provider, alongside `max_chars_per_request` (all three
  documented in `providers/translation/base.py`, read with `getattr()`, same convention as
  `translate_document()`). Decision 6 left the plumbing unspecified.

**Found while deploying the receiver, all fixed and recorded in its README:**

- `export default` in a Vercel `api/` file is invoked the Node way, `(req, res)`, so `request.url`
  is only a path and `new URL()` throws. Named `GET`/`POST`/`DELETE` exports receive a standard
  Web `Request`.
- `@vercel/blob`'s `get()` returns a plain `ReadableStream`; Vercel's own docs show
  `stream.text()`, which does not exist on one. The offline tests missed it because they use an
  in-memory store, so the stream read is now an isolated, tested helper (`lib/stream.js`).
- **`vercel deploy` attaches the repo's git author, and Vercel blocks a deployment whose author
  is not a member of the team.** The account owner's git identity differs from the Vercel
  account's, so the second deploy was blocked ("the commit author doesn't have permission to
  create deployments for this project") although the first had passed. The receiver is deployed
  from a copy outside the git repository, which has no author to check. A dashboard redeploy
  re-runs the same source, so it can change settings but not ship code.
- A runtime log line printed a request URL containing the shared secret, so the secret was
  rotated after the first test. The CLI never puts the receiver URL in an error message for the
  same reason.

**Real runs against eTranslation (same day), on the `books/test` chapter (14,740 characters):**

- **It works end to end.** Submit, delivery through the receiver, decode, write, ledger entry and
  S3 dashboard row (`provider: etranslation`, `usage.characters`, `cost_usd: 0.0`) all behave as
  designed, and the resume file is removed once the output is written.
- **Turnaround was 63 s and 33 s** for that chapter. A full-length book's turnaround is still
  unknown, so the polling defaults remain guesses, but they look generous at this size.
- **Markup survived with `document_format: txt`.** Line count (75 in, 75 out), the two-line
  chapter heading, `[i]kang[/i]`, `[sc]VIII[/sc]` and an `[FN: ...[/FN]]` footnote (with its text
  translated) all came back intact, and nothing HTML-shaped leaked. **This changed the default
  from Decision 5's `html` to `txt`**: it is the only format proven with real calls, it needs no
  conversion code in the path, and it is what the Commission calls its most efficient format. The
  `html` mode is kept and tested offline, but no real call has exercised it.
- **`llm_enhanced: true` did nothing on `GEN`, and the reason is the domain.** The account's
  `GET /getDomains` shows EN to ES as glossary-only on `GEN` and `IPO`, plain on `QE`, and with an
  LLM variant only on `SPD`, `ECB` and `ECJ` (specialist, institutional domains). A second run
  with the flag set produced a translation identical to the first except for the two lines whose
  input had been edited. eTranslation ignores the flag silently, so the provider now reads
  `getDomains` once per run when `llm_enhanced` is true and warns, naming the domains that do
  have an LLM variant. It never stops a run.
- **`GEN` supports a glossary** (`EN-ES-GLS`), which makes the glossary feature deferred in "What
  does NOT ship" feasible on the domain this project uses, without giving up the default.

**Quality, stated plainly.** On the one chapter compared against the earlier DeepL-derived text
(the `ortho` output, so not a perfectly clean baseline), eTranslation's plain machine
translation was noticeably more literal: *asses* became *culos*, *bolted* became *atornillada*,
*walls* became *paredes*, a heading word (*RUMOURS*) was left untranslated, and *five hundred
yards* became *500 metros* — a silent unit conversion that no later step can catch, since `ortho`
and `copyedit` never see the English. The account owner's own reading was "OK-ish". The
accepted trade is a free service in exchange for a closer read of the output; DeepL stays
selectable. One chapter is a small sample, and the specialist LLM domains have not been tried at
the time of writing.

**Overwrite incident, for the record.** `s1b translate` writes to the same path as the input's
name, so the first real run replaced `books/test/s1b/translated/es/zayagan-chp1.txt`, the raw
DeepL translation earlier ADRs reused as an `ortho` fixture. The `ortho/` and `copyedit/` outputs
derived from it were untouched; the raw DeepL text is gone. The run was started on the author's
instruction without warning of this.

**Verified:**

- The receiver, on the real deployment, against the real private store: a request with no token
  gets `401`; a write, a read-back, a delete and a second read-back behave as designed.
- The provider, with real calls: submit, delivery, decode and write in `txt` mode, markup
  preservation as above, ledger and dashboard recording.
- Unit tests, all offline: the receiver's handler (10 Node tests) and the provider, markup
  conversion, resume, LLM-support warning, chunk size, pricing and factory validation (39 Python
  tests).

**Not verified, and not to be read as settled:**

- **Resume after a real interruption** (it is covered by offline tests only).
- **A full-length book:** turnaround, and whether one request of that size behaves like a chapter.
- **The `html` document format** against the real service.
- **The LLM-enhanced domains** (`SPD`, `ECB`, `ECJ`) on this material.
- The "pickup" delivery mode in the error-code list remains undocumented and unasked about.
