# ADR 019 — System 1C: PDF Print-Prep Tooling (TrimBox/BleedBox)

**Status:** Implemented.

---

## Context

Signing up with Bibliomanager (a second print-on-demand distribution platform, alongside
IngramSpark) surfaced a real technical gap: its PDF requirements differ from IngramSpark's, and
neither is met by the file this project's automated pipeline currently produces. `s1b format`
(ADR 001) exports a formatted `.odt` from the copy-edited manuscript; from there, a human opens
it in LibreOffice, finalizes layout, reviews it, and exports it to PDF. LibreOffice's PDF export
only ever writes `/MediaBox` — it never defines `/TrimBox` or `/BleedBox` as explicit page-box
keys, which print vendors' prepress checks expect to find.

`docs/vsm.md` already names this territory — System 1C, "Design and Graphic Production" —
explicitly covering "Preparation of files for print (PDF/X)... Delivery of final files to
printer and digital platforms." 1C has no code anywhere in this project; it's pure manual/
freelance work by design, and the automated pipeline deliberately stops at `s1b format`'s `.odt`
output (labeled "1B → 1C handoff" in the README's task table). Layout finalization, human
review, and PDF export all happen after that handoff, outside System 2 orchestration — the same
posture ADR 011 already established for sales-report ingestion ("no portal automation... manual
download and drop-in is fine, same as every other 'no background process' step in this
pipeline").

A standalone script, `pdf_bleed_tool.py`, was written to close the TrimBox/BleedBox gap using
`pikepdf` (no Acrobat/InDesign required): `check` inspects a PDF's page boxes against an
expected trim size; `set-interior` writes an explicit no-bleed TrimBox for the interior PDF;
`set-cover` writes a centered TrimBox plus a full-page BleedBox for the cover PDF, optionally
folding in a vendor-calculated spine width.

## Decision

### 1. This is System 1C tooling, not a System 2 task

`pdf_bleed_tool.py`'s logic stays System 1C's first piece of tooling: invoked by hand, after a
human has already reviewed the exported PDF, immediately before upload to a distribution
platform. It is **not** a manifest task and writes nothing to `manifest.yaml`/the S2 ledger —
its input (a manually reviewed, manually exported PDF) isn't tracked state and doesn't come from
an engine, so folding it into ledger-tracked orchestration would misrepresent a deliberate human
checkpoint as an automated pipeline stage. This mirrors ADR 011's `sales-ingest` reasoning almost
exactly, except 1C doesn't even get a task-shaped command the way `sales-ingest` did — there's no
LLM call, no engine, nothing for `task_loader` to record.

### 2. Exposed through `pipeline.py s1c`, but the logic lives in its own module

Unlike a plain root-level standalone script with no relationship to the rest of the CLI,
`pipeline.py` gains a hand-written `s1c` Click group (`check`, `set-interior`, `set-cover`)
mirroring `pdf_bleed_tool.py`'s own subcommands and flags, so every VSM system is reachable the
same way, from one entry point:

```bash
uv run python pipeline.py s1c check exported_cover.pdf --trim-w 152 --trim-h 229
uv run python pipeline.py s1c set-interior interior_in.pdf interior_out.pdf --trim-w 152 --trim-h 229
uv run python pipeline.py s1c set-cover cover_in.pdf cover_out.pdf --trim-w 304 --trim-h 229 --spine-w 12 --bleed 3
```

`pdf_bleed_tool.py` itself stays a separate, importable module rather than being folded into
`pipeline.py` — its three operations (`cmd_check`, `cmd_set_interior`, `cmd_set_cover`) already
took a plain object with named attributes, not anything argparse-specific, so `pipeline.py`'s
Click commands just build a `types.SimpleNamespace` with the same field names and call straight
into them. Its own `argparse`-based `__main__` entry point is left intact and still works
identically, standalone, for quick debugging without going through `pipeline.py`.

### 3. Scope: three subcommands, nothing more

- `check` — inspect a PDF's page boxes (MediaBox/TrimBox/BleedBox) in mm, optionally comparing
  against an expected trim size within a tolerance (0.4mm default, a common vendor requirement).
- `set-interior` — write an explicit, no-bleed TrimBox on every page of an interior PDF, warning
  (not failing) if the page size doesn't match the expected trim within tolerance.
- `set-cover` — write a centered TrimBox and a full-page BleedBox on a cover PDF, with an
  optional vendor-calculated spine width folded into the trim width.

## Alternatives Considered

- **Fold `pdf_bleed_tool.py`'s logic directly into `pipeline.py`** — rejected: no benefit over
  keeping it a separate module that `pipeline.py` imports, and it would remove the standalone
  debugging path (`python pdf_bleed_tool.py check ...`) for no reason.
- **Leave it a fully standalone script, invoked directly, with no `pipeline.py` integration at
  all** — reconsidered and rejected: this project treats `pipeline.py` as the single entry point
  for every system (S1B, S1D, S2, S3, S4, S5), and a second, unrelated root-level entry point
  would break that consistency for no real gain, since nothing about wiring it through `pipeline.py`
  requires touching the ledger.
- **Make it a manifest task under a new `systems/s1c/` directory** — rejected: manifest tasks are
  for engine-dispatched, ledger-recorded work with tracked input/output (ADR 001). This script's
  input is a manually reviewed, manually exported PDF with no place in that graph; forcing it in
  would misrepresent a deliberate human checkpoint as an automated pipeline stage.
- **A general per-platform config abstraction (e.g. named Bibliomanager/IngramSpark trim/bleed
  presets)** — not built now: with a single real second platform and no evidence yet of how
  often specs actually differ in practice, this would be speculative generality ahead of a real
  need. Revisit if a third platform, or frequent per-book parameter changes, make typing
  `--trim-w`/`--trim-h`/`--bleed` by hand repeatedly at the CLI actually painful.

## Consequences

**Easier:**
- Print vendors that reject or mis-trim a PDF missing explicit TrimBox/BleedBox keys (Bibliomanager's
  stated requirement, and good practice for IngramSpark too) are handled without Acrobat or
  InDesign.
- `pipeline.py --help` now lists every VSM system with implemented tooling, System 1C included,
  instead of silently skipping the one system that has any code at all but wasn't reachable
  through the CLI.

**Harder / needs care:**
- 1C otherwise remains entirely manual (cover design, interior layout, the PDF export itself) —
  this ADR closes one specific technical gap, not System 1C as a whole. Cover design generation
  and full prepress automation are explicitly out of scope, per the project's existing
  next-steps assessment ("standardize a series cover template first — a design decision, not a
  pipeline one").
- Portal upload (System 1D's "Upload final files... to the print-on-demand portal" per
  `vsm.md`) is unaffected and stays manual, same as every other portal-facing step in this
  project.

---

## Implementation Checklist

- [x] Add `pikepdf` to `pyproject.toml` dependencies (`uv add pikepdf`), locked in `uv.lock`
- [x] Add `s1c` Click group to `pipeline.py` (`check`, `set-interior`, `set-cover`), calling into
      `pdf_bleed_tool.py`'s existing `cmd_check`/`cmd_set_interior`/`cmd_set_cover` via
      `types.SimpleNamespace` — no changes to `pdf_bleed_tool.py`'s own logic
- [x] Confirm the standalone `python pdf_bleed_tool.py ...` invocation still works unchanged
- [x] Update `pdf_bleed_tool.py`'s module docstring to note its System 1C status and the
      `pipeline.py s1c` entry point
- [x] Add a `pipeline.py s1c check` usage example to `pipeline.py`'s own module docstring
- [x] End-to-end smoke test: `check`/`set-interior`/`set-cover` against synthetic PDFs via both
      `pipeline.py s1c ...` and the standalone script, confirming identical output, correct
      TrimBox/BleedBox values, and the expected mismatch warning when page size doesn't match
      the given trim+bleed
- [x] Document the manual System 1C workflow step in README.md
