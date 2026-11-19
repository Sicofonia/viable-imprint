# ADR 025 — Deterministic OCR Pre-pass for System 1B, a Slimmer `cleanup`, and Italics Moved to `ortho`

**Status:** Implemented (2026-11-19). Run offline on the whole of *hunted-central-asia* (see Implementation Notes); the first real `s1b cleanup` on a pre-passed book is pending the Editorial Director.

---

## Context

`s1b cleanup`'s prompt asked an LLM to do four mechanical jobs on every chunk of an OCR'd book — fix line breaks, delete page apparatus, rejoin hyphenated words, and keep paragraph breaks — and two jobs it cannot do at all, because the input carries no information about them: mark italics and small capitals (`[i]`, `[sc]`) in a plain-text OCR with no style data.

A review of the first quarter of *hunted-central-asia*'s source (a 1932 text in a 1993 edition; 14,000 lines) showed how badly that fits a real scan:

- **Page apparatus is perfectly regular.** Every page is `body lines, page number, blank line, RUNNING HEADER, body lines`. Odd pages carry the book title, even pages the chapter title. Of 321 pages, 296 numbers are clean integers and 12 are mangled (`Be)`, `IL`, `i &`, `I41`…). This is a job for code, and an LLM doing it chunk by chunk can neither check the sequence nor tell a running header from a real chapter heading.
- **Blank lines are page breaks, not paragraph breaks.** The chapters are one sentence per line; the Introduction is hard-wrapped; the paragraph indents did not survive OCR. `lib/chunker.py` splits on `\n\n`, so today it chunks by *page*, and cleanup's rule "preserve blank lines" would keep every page break as a false paragraph break in the middle of a sentence. Translate, ortho, copyedit and `odt_writer` all treat a blank line as a paragraph.
- **Hyphens:** 539 line-end hyphens. 382 resolve from the book's own vocabulary (the joined word appears solid elsewhere), 19 are attested real hyphens, about 125 are genuinely ambiguous (`publish-ing` vs `fourteen-year-old`).
- **Chapter headings are the worst-damaged part of the scan:** of 20 chapters, 7 have headings mangled beyond parsing (`CRAP / TRE Riv. I.`, `CHA PLE / Rexel eee / SOFAS LON.`), and the raw numerals are unreliable (`IL.` for I, `XV.` for XIV). Their titles survive, clean and repeated, in the running headers of the pages that follow.
- Also unhandled by the old prompt: spaced `? ; !` (285), garbled quote marks (`‘“‘`, `”’`, 278), split ellipses (71 lone `.` lines), `Tf`/`T am` for `If`/`I am`, chapter lead-ins in capitals (`ONE evening`), page-foot footnotes with mangled markers.

## Decision

### 1. A deterministic pre-pass, `pipeline.py s1b prepass`, outside the ledger and outside `s2`

Same posture as `s1c` (ADR 019) and `sales-ingest` (ADR 011): no LLM, no engine, nothing in the `tasks:` ledger, not run by `s2 run`. It is a utility the Editorial Director runs by hand, once per source: `s1b prepass <source>` writes `s1b/prepass/<same name>`, and `s1b cleanup <that file>` takes it from there. The source is never modified. The command is hand-written on the generated `s1b` group (the `s5 check-policy-sync` arrangement), and the logic lives in `lib/ocr_prepass.py`, as the project does for every utility.

Written to `s1b/prepass/`, not `s1b/source/`, because `orchestrator._source_file` requires exactly one file in `source/`. Consequently `s2 run` still starts from the raw source and ignores the pre-pass; this is documented, not hidden, and `s2` was deliberately left unchanged (decided with the Editorial Director, 2026-11-19).

### 2. What the pre-pass does, in order

1. Normalise newlines, BOM, no-break spaces, trailing whitespace.
2. Split into pages at blank lines. Remove the page number (clean, or a short mangled fragment with a digit/symbol; a word-fragment such as `In-` is never taken for one) and wrap a page-foot footnote as `[FN: … [/FN]]`, in place.
3. Confirm running headers: a first-of-page all-caps line is a header only if it repeats at least twice or equals a known section title. Unconfirmed caps lines are kept as their own block and reported. A confirmed header stranded mid-page (its page break lost) is removed too.
4. Chapter headings: parse `CHAPTER / numeral / TITLE` where it is clean (`GHAPTER` etc. tolerated); snap the title to the running-header title; for chapters whose heading is mangled, find the chapter-start page from the first appearance of its title as a running header and replace the debris. Chapters are numbered **by order of appearance**, not by the raw numeral, and every disagreement or repair is listed in the report. A running header is taken for the book title (and not for a chapter) when it is interleaved with other titles, is the only kind, or is on at least half the header pages — when unsure it is *not* a chapter, since a wrong guess would invent one.
5. Join line-end hyphens: join if the joined word is attested solid elsewhere in the book; keep if the hyphenated form is attested, the first part is a compound prefix (`self-`, a number word…), or the second part is capitalised or already hyphenated; otherwise **join with the marker `{~}`**. Hyphens inside a line (`with- out`) are treated the same way.
6. Join lines into running text; repair ellipses, spaces before `? ; ! :`, garbled quote runs (by the direction of their glyphs) and a four-entry table of certain misreadings (`Tf`, `Tt`, `T`+verb, `|`+verb). Nothing else is guessed.
7. Sentence-case chapter lead-ins (`ONE evening` → `One evening`).
8. **Provisional paragraph blocks**: a blank line is proposed only at a line end that ends a sentence (abbreviations such as `Mr.` and initials are not sentence ends), before a line that opens a quotation (a dialogue turn), after a short last line in a hard-wrapped run (detected per run from line-width regularity), and as a forced split near 1,500 characters so no block is unbounded. Never mid-sentence. This also keeps `chunk_by_paragraphs` working: without any `\n\n` the whole book would be one chunk.

A report (always a summary on stdout; `--report` writes an audit log) lists every removed line, how each chapter heading was read, the hyphens left as `{~}`, page-number gaps, and every caps line that was kept because it could not be confirmed as a header. Running it on its own output changes nothing.

### 3. `cleanup`'s prompt shrinks to what only an LLM does well

`prompts/s1b/cleanup_task.txt` (and `prompts/examples/s1b/cleanup_task.txt`) now ask for exactly four things: decide each provisional paragraph break (keep, remove, or add inside a block, never splitting a sentence or moving words); resolve every `{~}` (soft hyphen → close up; real compound → `-`); fix context-dependent OCR errors and delete junk tokens; move each wrapped footnote next to the paragraph holding its mark. The period-spelling, proper-noun, punctuation and completeness rules stay. The rule list is also corrected: the duplicated `1→l`/`1→I` entry and the no-op `fi→fi`/`fl→fl` are gone.

`cleanup` gains `min_output_ratio: 0.9` in `systems/s1b/tasks.yaml`. It was exempt only because page apparatus shrank its input; that now happens before the LLM, so a reply under 90% of the input means text was dropped (the same guard ortho and copyedit already have, added there after the *the-gobi-desert* run that lost a chunk).

### 4. Italics (and small caps) move from `cleanup` to `ortho`

An OCR text has no style information, so `[i]` and `[sc]` in `cleanup` were never transcription, only guesses made in English before translation, then carried through a translation provider as literal text. They are orthotypographic work, and `ortho` already did part of it (`[i]` for foreign terms and ship names, `[I]` for roman numerals). Now `cleanup` adds no markup and `ortho` is the single place that adds `[i]`, extended to scientific Latin names, titles of books, journals and newspapers, and transliterated local terms left untranslated — marking clear cases only. `[sc]` is no longer produced anywhere (`odt_writer` never rendered it; it stayed literal text by design). `[FN: …]` is untouched: footnotes are structure, not typography, and stay in `cleanup`.

## What does NOT ship in this ADR

- **Any `s2` integration.** `s2 run` does not run, detect or prefer the pre-pass output. Making `s2` use `s1b/prepass/<name>` when present is a small change to `orchestrator._source_file`, deliberately postponed.
- **A text-preservation check** between the pre-passed text and the cleaned text (whitespace-normalised word comparison). `min_output_ratio` catches truncation, not substitution.
- **A per-book glossary for OCR-damaged proper names** (`Pashk6é`, `Ferghand`/`Ferghandé`, `Nazdroff`). `cleanup`'s proper-noun rule still tells the LLM to leave names as written, which freezes OCR damage in names; that needs a glossary mechanism, not a prompt tweak.
- **Italics recovery from the scan.** The OCR text has none; if a future source carries style information, it would be handled before this step.

## Alternatives Considered

- **Make the LLM prompt better and keep it all in `cleanup`.** Rejected: 90% of apparatus and hyphen decisions are checkable in code, the LLM sees one chunk at a time and cannot confirm a header repeats or a chapter is numbered in sequence, and every LLM call on a mechanical task costs quota and can drop text.
- **A second LLM task for OCR.** Rejected for the same reasons, and `llm_text` would still need paragraphs the source does not have.
- **A pre-pass that is a manifest task.** Rejected: its input and output are not stages of the book's pipeline in the sense `s2` tracks, the user wants to run it by hand, and putting it in `tasks.yaml` would make it `s2`'s first step (ADR 009 would then cascade-invalidate the whole book when it is re-run).
- **Paragraph breaks decided only by the LLM, or only by code.** Rejected: only-LLM produces unbounded blocks, an unsplittable chunker input and no way to tell a truncated reply; only-code gives walls of text. The hybrid bounds every LLM decision to a block of about 1,500 characters.
- **Numbering chapters by the raw numeral.** Rejected on the evidence: in this book one readable numeral disagrees with the chapter's order (`XV.` on the fourteenth), one is unreadable (`IL.` for I), and seven chapters have no usable numeral at all, so a numeral is neither reliably right nor reliably present.

## Consequences

- A scanned book now goes `s1b prepass` → `s1b cleanup <prepass file>` → the rest. The prompt change is local (`prompts/s1b/` is gitignored); `prompts/examples/s1b/` carries the tracked version.
- A book already cleaned with the old prompt keeps its `[i]`/`[sc]` markup; `ortho` will not remove it, and `odt_writer` still renders `[i]`. Re-cleaning an old book is not needed.
- If the pre-pass mis-reads a chapter heading, the report says so (`Chapter headings that need a human look`) and the damage is confined to that heading block.
- The quote repair collapses runs such as `”’` to one closing mark, which would also collapse a legitimate nested close (`.’”`) in a book that uses nested single quotes. The report counts repaired runs so this can be checked; the source stays untouched.

## Implementation Notes (2026-11-19)

Written and measured against the whole of *hunted-central-asia* (584 KB, 13,996 lines) before any LLM call:

- 321 pages; 296 clean page numbers and 12 mangled ones removed, 16 pages without a number; 22 page-number sequence gaps reported (OCR errors, not missing pages).
- 296 running headers removed (146 of them the book title), including one stranded mid-page that had split a sentence in the first version.
- 20 chapters found: 13 parsed directly, 7 recovered from their running headers; 9 reports flagged for a look (7 repairs, one unreadable numeral, one numeral that disagrees with the order).
- Hyphens: about 388 joined, 40 kept, 111 marked `{~}`.
- 59 ellipses, 197 quote runs and 532 spacings repaired; 26 certain OCR misreadings fixed.
- About 620 provisional blocks, mean 880 characters, none above 2,500; 3.4% of the text removed. Second run on the output: byte-identical.

Remaining for the Editorial Director: run `s1b cleanup` on `s1b/prepass/source.txt.txt` and check that no `{~}` is left (`grep -c "{~}"` must print 0), that the paragraphing reads right, and that the seven repaired chapter openings (listed in the report) look correct.
