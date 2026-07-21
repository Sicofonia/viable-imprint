# ADR 013 — System 5: Hardening (Policy-Drift Tripwire and Verdict Calibration Log)

**Status:** Proposed. Not yet implemented — for review before work starts.

---

## Context

A private follow-up review of the implemented project (`~/.claude/viable-imprint-next-steps.md`) named two small, specific gaps left over from ADR 006, both explicitly flagged there as accepted risk rather than fixed:

> **S5 hardening — two cheap additions**
> - **Policy-drift tripwire:** the evaluation prompt is a hand-maintained copy of `vsm.md`'s System 5 section (ADR 006 point 7's accepted risk). Add a tiny test that hashes that section of `docs/vsm.md` and fails when it changes, forcing the question "was `prompts/s5/policy_evaluation_task.txt` updated too?" ~5 lines; converts an annual memory burden into a mechanical prompt.
> - **Verdict calibration log:** verdicts are advisory and nothing records whether the Director agreed. Append one line per evaluated candidate (agent verdict / human decision / date) to a log under `candidates/`. After ~a dozen candidates this becomes the only empirical basis for the project's central question: when can a review loop be relaxed?

Both trace directly to specific points ADR 006 already wrote down as unfinished business, not new scope:

- Point 7's own words: *"the prompt is a hand-maintained copy of `vsm.md`'s policy, not a live read of it — if the annual policy revision happens and nobody remembers to update the prompt, the agent silently arbitrates against a stale policy. No code catches this."*
- Point 6's own words, about the verdict staying plain Markdown rather than tagged: *"If a future need for machine-parsing arises (e.g. a candidates dashboard), XML-tagging the verdict line then is the same reactive move ADR 002 made for `brief` — not done speculatively now."* This ADR is that future need arriving.

**Checked before designing anything, not assumed:** the real `prompts/s5/policy_evaluation_task.txt` (read directly, not from memory) draws specifically from `vsm.md`'s **`### Editorial Policy (Constitutive Criteria)`** subsection — thematic scope in/out and non-negotiable values — not the whole `## System 5` section, which also contains Description, Mission, Responsibilities, Practical Expressions, and Qualitative Metrics that the prompt doesn't mirror verbatim at all. Hashing the whole `## System 5` section, as the review's own wording loosely suggests, would fire false-positive drift warnings whenever an unrelated part of that section changes (e.g. a Qualitative Metrics bullet) — this ADR hashes the narrower, actually-relevant subsection instead.

---

## Decision

### 1. Policy-drift tripwire: a stored hash inside the committed prompt file, checked by an explicit command

`prompts/s5/policy_evaluation_task.txt` is already the one prompt in this project that's committed, specifically *because* it's substantively the same content as `docs/vsm.md`'s public policy section (ADR 006 point 7). Its existing leading comment block (stripped before reaching the model, per `load_prompt()`'s maintainer-comment convention) gains one more line — a short hash of the `### Editorial Policy` subsection's text, as of the last time a human confirmed the two were in sync:

```
# Mantener sincronizado a mano con docs/vsm.md -> sección "System 5 —
# Identity, Values and Policy" ...
# vsm-sync-hash: 4f9a2c1e0b7d
```

New `lib/policy_check.py`:

```python
import hashlib
import re
from pathlib import Path

_SECTION_START = "### Editorial Policy (Constitutive Criteria)"
_HASH_LINE = re.compile(r"^# vsm-sync-hash:\s*([0-9a-f]+)\s*$", re.MULTILINE)


def extract_policy_section(vsm_path: str = "docs/vsm.md") -> str:
    """The exact subsection prompts/s5/policy_evaluation_task.txt is derived
    from — from "### Editorial Policy (Constitutive Criteria)" up to the next
    "###"/"##" heading, whichever comes first. Raises if the heading isn't
    found at all (vsm.md restructured more than expected — fail loud, don't
    silently hash the wrong thing or an empty string).
    """
    lines = Path(vsm_path).read_text(encoding="utf-8").splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == _SECTION_START), None)
    if start is None:
        raise ValueError(f"Could not find {_SECTION_START!r} in {vsm_path} — has vsm.md been restructured?")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("#")), len(lines))
    return "\n".join(lines[start:end]).strip()


def current_hash(vsm_path: str = "docs/vsm.md") -> str:
    return hashlib.sha256(extract_policy_section(vsm_path).encode("utf-8")).hexdigest()[:12]


def stored_hash(prompt_path: str = "prompts/s5/policy_evaluation_task.txt") -> str:
    text = Path(prompt_path).read_text(encoding="utf-8")
    match = _HASH_LINE.search(text)
    return match.group(1) if match else None
```

New hand-written command, `pipeline.py s5 check-policy-sync [--update]`, added to the existing `_s5_group` alongside `log-decision`:

- No flag: compares `current_hash()` against `stored_hash()`. Match → prints a plain confirmation, exits 0. Mismatch (or no stored hash at all) → prints a loud, explicit warning naming both hashes and pointing at `docs/vsm.md`'s `### Editorial Policy` subsection and the prompt file, exits 1 (the "fails" the review asked for — this is a standalone check command, not something that blocks an editorial workflow, so failing loud here costs nothing a human wasn't already going to read).
- `--update`: recomputes `current_hash()` and rewrites the `# vsm-sync-hash:` line in the prompt file, printing what changed. Run only after a human has actually confirmed the prompt itself was updated to match — this command does **not** touch the prompt's real content, only the sync marker, and doesn't verify the human actually did the sync (it can't; that's still their judgment call).

### 2. Verdict calibration log: tag just the verdict value, nothing else

ADR 006 deliberately kept `s5 evaluate`'s whole verdict as plain Markdown, since nothing parsed it. Calibration needs exactly one value machine-readable — which of "Encaja" / "Caso límite" / "No encaja" the agent returned — so `prompts/s5/policy_evaluation_task.txt`'s `## Veredicto` section gains a tag around only that value, the other three sections staying exactly the free-form human-read prose they already are:

```
## Veredicto
<verdict>[Encaja / Caso límite / No encaja]</verdict>
```

New `candidates/calibration.yaml` — flat, append-only, same shape as `homeostat/decisions.yaml` (ADR 007):

```yaml
- candidate: life-of-a-nomad
  agent_verdict: "Encaja"
  human_decision: acquired
  date: "2026-07-21"
```

`human_decision` is free text, not a constrained enum, matching this project's standing preference for human-authored fields it doesn't need to parse further (candidate briefs themselves are free-form prose, ADR 006 point 5) — `acquired`/`declined`/`deferred` are suggested in the command's help text, not enforced.

New command, `pipeline.py candidate record-decision <slug> <decision>`, added to the existing `candidate` group:

```python
@candidate.command(name="record-decision")
@click.argument("candidate_slug")
@click.argument("decision")
@click.pass_context
def candidate_record_decision(ctx, candidate_slug, decision):
    """Record what the Director actually decided, against s5 evaluate's own
    verdict, for calibration (candidates/calibration.yaml). DECISION is free
    text — e.g. acquired, declined, deferred."""
    root = paths.candidates_root(ctx.obj["config"])
    verdict_path = root / "s5" / "evaluate" / f"{candidate_slug}.txt"
    if not verdict_path.exists():
        raise click.ClickException(f"No verdict found at {verdict_path} — run `s5 evaluate` for this candidate first.")
    match = re.search(r"<verdict>\s*(.*?)\s*</verdict>", verdict_path.read_text(encoding="utf-8"))
    agent_verdict = match.group(1) if match else "(sin etiqueta <verdict>)"
    candidates.record_calibration(root, candidate_slug, agent_verdict, decision)
    click.echo(f"Recorded: {root / 'calibration.yaml'}")
```

`lib/candidates.py` (new, tiny) gains `record_calibration(root, candidate_slug, agent_verdict, human_decision)`, structurally identical to `lib/homeostat.py`'s existing `record_decision()` — load, append one dict with today's date, save.

**Deliberately not built here: any automatic agreement/accuracy computation** (e.g. "the agent and the Director agreed N% of the time"). The review's own framing is that the log *becomes* the empirical basis for a future question, after roughly a dozen entries exist — there's nothing to compute yet with zero real entries, and a computed metric over too small a sample would be more misleading than no metric at all. A human reads `calibration.yaml` directly for now; a dashboard reading it is a natural, separate future ADR once there's enough data for one to say anything real.

---

## Alternatives Considered

- **Running the drift check automatically on every `s5 evaluate` invocation, instead of a separate command** — considered, rejected: `evaluate`'s command is auto-generated by `lib/task_loader.py`'s generic `_build_command()`/`build_system_group()`, shared unmodified across every system with implemented tasks (`s1b`, `s1d`, `s4`, `s5`). Hooking a System-5-specific pre-check into that shared, generic mechanism would mean special-casing code every other system also depends on, for one system's specific need — against this project's own precedent of keeping `task_loader.py`/`orchestrator.py` generic and pushing system-specific behavior into hand-written additions (`s5 log-decision`, `candidate new`, `s3 sales-ingest` all already work this way). A separate, explicit command is a smaller, safer diff, and this project already accepts "a human remembers to run a small command" elsewhere (`s5 log-decision` isn't automatic either).
- **Hashing the whole `## System 5` section rather than just `### Editorial Policy (Constitutive Criteria)`** — rejected after checking the real prompt file directly against `vsm.md` (see Context): the prompt doesn't mirror Responsibilities/Practical Expressions/Qualitative Metrics at all, so hashing them would produce drift warnings the prompt genuinely doesn't need to act on.
- **Dynamically reading `vsm.md` at evaluation time instead of a hand-maintained prompt copy** — this is ADR 006 point 7's own already-rejected alternative (kept the standalone, LLM-tunable prompt file); this ADR doesn't reopen that decision, only adds a check that the accepted risk from keeping it hasn't quietly materialized.
- **A full `pytest`-based test suite, matching the review's literal word "test"** — rejected, consistent with ADR 009's identical reasoning: this project has no test framework anywhere, and introducing one as a side effect of a five-line hash check would be a disproportionate new dependency. A plain CLI command with a deterministic pass/fail exit code delivers the same "fails when it changes" mechanism without one.
- **XML-tagging the entire verdict section (all four headings), not just the verdict value** — rejected: only the verdict value needs code-side extraction; the other three sections are still exclusively human-read reasoning, and ADR 002/003's own established rule is to reserve tags for exactly the content that's actually parsed, nothing more.
- **Computing agent/human agreement automatically as part of this ADR** — rejected (Decision point 2): premature with zero real data, and the review's own framing treats the log itself, read by a human, as the deliverable at this stage, not a metric.
- **A constrained enum for `human_decision` (e.g. `acquired`/`declined`/`deferred` only)** — rejected: matches candidate briefs' own free-form-prose precedent (ADR 006 point 5); real editorial decisions may need nuance a fixed enum would flatten, and nothing downstream parses this field yet that would require it.

---

## Consequences

**Easier:**
- The exact risk ADR 006 named and accepted — a silently stale policy prompt after `vsm.md`'s annual revision — now has a mechanical, five-second check instead of relying on memory alone.
- `s5 evaluate`'s verdicts start accumulating a real evidence trail the moment this ships — the first real empirical input to this project's still-open, project-wide question of when a human review loop can be relaxed (named as a general open question in the review, not just an S5-specific one).
- Both additions are genuinely small: one new tiny module, two small hand-written commands added to command groups that already exist, one prompt line, one prompt tag. No engine changes, no new perpetual root (both live inside `candidates/`, already established by ADR 006).

**Harder / needs care:**
- The drift tripwire only fires when a human remembers to run `s5 check-policy-sync` — same "memory burden," just cheaper to discharge once remembered, not eliminated the way an automatic hook would (rejected above for a different, load-bearing reason: not touching shared generic code). Worth a note in the annual-review checklist `vsm.md` already prescribes, not just in this ADR.
- `--update` only rewrites the sync marker — it cannot verify a human actually reconciled the prompt's content with `vsm.md`'s new policy text first. It trusts the person running it, the same trust this project already places in every other advisory, human-reviewed step.
- The calibration log needs real volume (the review's own "~a dozen candidates") before it says anything meaningful — a sparse log is neither validating nor invalidating anything yet, and shouldn't be over-read this early.
- `agent_verdict` in the log is only as good as the `<verdict>` tag surviving reliably — this project's own prompt-reliability history (Rules 1–3, 9 in the shared feedback memory) says a first real run is the actual test of that, not the prompt's design.

---

## Implementation Checklist

- [ ] Add `lib/policy_check.py` (`extract_policy_section()`, `current_hash()`, `stored_hash()`)
- [ ] Add the `# vsm-sync-hash: ...` line to `prompts/s5/policy_evaluation_task.txt`'s leading comment block, computed from the real, current `docs/vsm.md`
- [ ] Add `pipeline.py s5 check-policy-sync [--update]` to `_s5_group`
- [ ] Wrap `## Veredicto`'s value in `<verdict>...</verdict>` in `prompts/s5/policy_evaluation_task.txt` and `prompts/examples/s5/policy_evaluation_task.txt`; add a rule noting the tag, matching how every other tagged prompt in this project explains its own tags
- [ ] Add `lib/candidates.py` (`record_calibration(root, candidate_slug, agent_verdict, human_decision)`), mirroring `lib/homeostat.py`'s `record_decision()`
- [ ] Add `pipeline.py candidate record-decision <slug> <decision>` to the existing `candidate` group
- [ ] End-to-end test: confirm `s5 check-policy-sync` passes against the real, freshly-hashed prompt; deliberately edit `docs/vsm.md`'s `### Editorial Policy` subsection (on a disposable copy or reverted after) and confirm the check fails loud and clearly; confirm `--update` correctly rewrites the stored hash; run `s5 evaluate` for a real or disposable candidate and confirm the `<verdict>` tag survives intact across at least two real runs (in-scope and out-of-scope cases, mirroring ADR 006's own original test pair); run `candidate record-decision` against that verdict and confirm `candidates/calibration.yaml` records correctly; confirm `record-decision` fails clearly if no verdict file exists yet
- [ ] Update README (System 5 section, command reference table, a note in Running the Pipeline about closing the loop on a candidate evaluation)
