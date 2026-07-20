# ADR 007 — System 5: Homeostat Dashboard (S3/S4 Confrontation)

**Status:** Proposed. Not yet implemented — for review before work starts.

---

## Context

A follow-up review of ADRs 004–006 (`~/.claude/viable-imprint-next-steps.md`, item 1 — highest priority of five) named the largest remaining gap precisely: *"S3's dashboard and S4's briefing are produced separately and nothing structurally confronts them."* Its proposal: *"Add a task (likely `s5` group) that generates a periodic (monthly) 'homeostat' document combining: the S3 portfolio dashboard output, the latest S4 briefing, and a closing section where the Editorial Director logs decisions against the editorial policy. This is Beer's S3–S4 homeostat with S5 arbitration, as a recurring document rather than a control room. Advisory synthesis only — it proposes tensions/tradeoffs to decide, it does not decide."*

This is, concretely, the System 3/4 conflict-arbitration half of System 5 that ADR 006 explicitly deferred (point labeled "aspect b" in that discussion) — `vsm.md` states System 5 *"arbitrates conflicts between System 3 ... and System 4 ... when their demands are contradictory,"* and ADR 006 only ever built the other half (candidate scope evaluation).

Discussing implementation, the question came up directly: should this be another Markdown document (matching every other synthesis output in this project so far), or an actual dashboard? Given the project is explicitly open-source and OS-agnostic, three shapes were weighed:

1. A static, self-contained HTML file generated fresh each run — no server, no new runtime dependency, opened by double-clicking, same mental model as opening any other generated document today.
2. The same, plus a lightweight decision-log affordance — the HTML renders decision *history*, and logging a new one stays a plain CLI command that appends to a file.
3. An on-demand local web app with a real form — genuinely more capable, but the first thing in this project resembling a running server rather than a one-shot command.

**Option 2 is what this ADR builds.** It earns the word "dashboard" without introducing a server this project has explicitly avoided twice already (ADR 003 point 6, ADR 004 point 1's "no scheduler, no daemon" framing) — it stays a one-shot command producing a file, exactly like every other artifact in this pipeline.

---

## Decision

### 1. Scope: confrontation and synthesis, not control

The homeostat page combines three already-existing sources of truth — nothing here invents new data:
- **System 3's portfolio snapshot** (`lib/dashboard.portfolio_summary()`, ADR 005 — already returns structured dicts, not just printed text)
- **System 4's latest briefing** (`intelligence/s4/briefing/<date>/combined.txt`, ADR 003)
- **The decision log** (new, this ADR — a flat, append-only record of what the Editorial Director has decided in past homeostat reviews)

An LLM pass reads all three and writes a short *tensions and tradeoffs* section — never numbers (those are rendered directly from structured data, never trusted to a model), never a decision (the prompt is explicit that this proposes questions, it doesn't answer them). Matching every other advisory output in this project: nothing here creates a book, changes a budget, or acts on anything. The Editorial Director reads the page and, separately, runs a CLI command to log what they decided.

### 2. Three pipeline stages, mirroring System 4's `scan` → `briefing` shape, plus one render step

System 4 already solved "gather structured/external material, then have an LLM synthesize it" with exactly two stages. This ADR needs a third, because rendering a dashboard needs the *raw structured data* (for a deterministic table and chart) as well as the *LLM's narrative* (for the prose) — those are two different serializations of the same underlying facts, not redundant work:

- **`s5 homeostat-scan`** (new engine, code only, `CLI_ARG = "none"` like `feed_scan`) — reads S3's portfolio summary, the latest S4 briefing file, and the decision log; writes a single tag-delimited `combined.txt`, mirroring `feed_scan._wrap_source()`'s `<fuente>` convention exactly (already proven to avoid the heading-priming failure ADR 003 point 10 found — see point 6 below).
- **`s5 homeostat`** (reuses `engines/llm_text.py` completely unchanged) — reads that combined file, writes a short Markdown *tensions and tradeoffs* narrative. Chunking is irrelevant here (`max_chars: 100000`, same as `s5 evaluate` and every `s1d` expansion task) — this input is never manuscript-length.
- **`s5 homeostat-render`** (new engine, code only) — reads the tensions narrative (its declared input file) plus, independently, a *fresh* call to `lib.dashboard.portfolio_summary()`, the same latest S4 briefing file, and the decision log; writes the final self-contained `homeostat.html`.

```yaml
# systems/s5/tasks.yaml — additions
  - name: homeostat-scan
    engine: homeostat_scan

  - name: homeostat
    engine: llm_text
    prompt: prompts/s5/homeostat_task.txt
    max_chars: 100000

  - name: homeostat-render
    engine: homeostat_render
```

**No scheduler is added** — wire this into cron/Task Scheduler yourself for a monthly cadence, identical advice to ADR 003 point 6. But unlike the original draft of this ADR, the three stages are *not* left as three commands to remember and sequence by hand — see point 7.

### 3. A fourth perpetual root — `homeostat/` — same shape as `intelligence/` and `candidates/`

Nothing here is book-scoped or belongs under `books/`. `lib/paths.py` gains `homeostat_root(config)`, a third copy of the exact same pattern as `intelligence_root()`/`candidates_root()`:

```python
def homeostat_root(config: dict) -> Path:
    root = Path(config.get("homeostat_dir", "homeostat")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.yaml"
    if not manifest_path.exists():
        manifest_path.write_text("slug: homeostat\n", encoding="utf-8")
    return root
```

```
homeostat/
├── manifest.yaml
├── decisions.yaml                  # flat, append-only — see point 5
└── s5/
    ├── homeostat-scan/
    │   └── 2026-08-01/
    │       └── combined.txt        # S3 + S4 + decisions, tag-delimited
    ├── homeostat/
    │   └── 2026-08-01/
    │       └── combined.txt        # LLM tensions/tradeoffs narrative
    └── homeostat-render/
        └── 2026-08-01/
            └── combined.html       # the actual dashboard
```

The date-folder mirroring is free — `lib/paths.stage_output_dir()` already treats the date segment exactly like a language code (confirmed working this way for System 4 in ADR 003), so `homeostat`'s and `homeostat-render`'s outputs land in matching dated folders with zero new path logic. Gitignored entirely, same treatment as `books/`, `intelligence/`, `candidates/` — this is working business data, not something to commit.

### 4. `homeostat-render` is a new, code-only engine — not `llm_text`, and not a markdown library

`llm_text.py` can't be reused for rendering: its output filename always mirrors the input's name (`combined.txt` in, `combined.txt` out), and it would try to make an LLM call, which this step must not do — the whole point is that the table/chart are computed, never generated. `homeostat_render.py` follows `odt_format.py`'s existing precedent instead (a code-only engine that computes its own differently-named output: `input_file.stem + ".html"`).

Converting the two Markdown documents it embeds (the `homeostat` narrative and the latest `s4 briefing`) to HTML doesn't need a new dependency. Both are written by prompts *this project controls*, in a known, narrow subset of Markdown (headings, paragraphs, bullet lists — see every `prompts/s1d/`, `prompts/s4/`, `prompts/s5/` prompt so far) — a small bespoke converter handling exactly that subset (mirroring `feed_scan.py`'s own precedent of a narrow, stdlib-only `_TextExtractor(HTMLParser)` rather than pulling in a general HTML library) is a better fit than adding a general-purpose Markdown package for formatting this project's prompts will never actually produce (tables, images, nested lists). See Alternatives.

The S3 portfolio data renders as a plain HTML `<table>` (same columns `s3 dashboard` already prints) plus one simple horizontal bar chart (API cost per book) as inline SVG generated directly in Python — no charting library, no CDN, no JavaScript at all. This keeps the page working from a bare `file://` URL, offline, on any OS, forever — no dependency on a network fetch succeeding just to render a chart.

### 5. The decision log: a flat YAML file, one command to append

```yaml
# homeostat/decisions.yaml
- date: "2026-08-01"
  tension: "S4 briefing flagged rising audiobook demand; S3 shows three titles already in production at full capacity."
  decision: "Deferred audiobook exploration to Q1 2027; no new title started this month."
```

A new hand-written command, `pipeline.py s5 log-decision "<tension>" "<decision>"`, appends one entry with today's date — added to the same `s5` Click group `build_system_group()` already returns (captured in a variable in `pipeline.py`, one `.add_command()` call before it's registered), the same way `candidate`/`s2`/`s3` already mix manifest-driven and hand-written commands under one CLI surface. No interactive form, no free-text file to open in an editor — these are short log lines, not discursive documents, so two CLI arguments are enough. `homeostat-render` reads this file fresh each run and shows the full history in the page, most recent first.

This is deliberately narrower than next-steps.md item 5's separate "verdict calibration log" idea for `s5 evaluate` — that's a different log, for a different task, and stays out of scope here.

### 6. The prompt: tag-delimited input (not `=== headers ===`), fill-in-template output, explicit non-decision framing

`homeostat-scan`'s `combined.txt` uses the exact same `<fuente nombre="..." estado="...">` delimiter `feed_scan.py` already established — reusing the mechanism proven (ADR 003 point 10) to avoid priming the model into inventing similarly-styled headings in its own output. Three tagged sections: `<s3_snapshot>` (the portfolio table, rendered as plain text), `<s4_briefing>` (the latest briefing's full text), `<decisiones_previas>` (the decision log, or a note that none exist yet).

`homeostat_task.txt`'s output template (fill-in-the-template style per ADR 002 points 6–7, literal headings the prompt requires reproducing verbatim):

```
## Resumen del período
[Lo ocurrido este período, combinando la actividad de System 3 y las señales de System 4.]

## Tensiones sistema 3 / sistema 4
[Dónde las señales de System 4 (oportunidades, riesgos) chocan con la capacidad o el coste actual que muestra System 3 — o donde no hay tensión real que señalar este período.]

## Preguntas para el Director
[Una lista concreta de tensiones o disyuntivas a resolver — nunca una recomendación ni una decisión. Si hay decisiones previas relevantes en el histórico, menciónalas como contexto.]
```

An explicit rule states the model proposes tensions and questions, never a recommendation or decision — mirroring `s5 evaluate`'s existing "you do not decide" framing (ADR 006 point 1) one level up, now applied to a portfolio-wide question instead of a single candidate.

### 7. `s2` integration — revised after review: three commands to sequence by hand is exactly the problem System 2 exists to remove

The first draft of this ADR left `homeostat-scan` → `homeostat` → `homeostat-render` as three commands to remember and run in order — reasoned about as "not book-scoped, so `s2` doesn't apply," the same call already made for System 4. Raised directly in review: that reasoning proves too much. The entire reason System 2 exists is that hand-sequencing commands doesn't scale past a couple of steps; three new ones on top of System 4's existing two and System 1B/1D's thirteen is exactly the growing pile ADR 004 was built to remove. Scope increased accordingly.

**Why this can't be `s2 run <book_slug>` as it already exists — two real differences, not just "different root":**

1. **System 5's tasks now span two different roots, not one.** `s1b`/`s1d` are uniformly book-scoped; `s4` is uniformly `intelligence/`-scoped. `s5` is not uniform: `evaluate` operates against `candidates/`, while the three homeostat tasks operate against `homeostat/`. The existing orchestrator's model — one system name implies one root — breaks the moment one system's `tasks.yaml` serves two different perpetual roots. Book-scoped orchestration (`_load_graph()`, `BOOK_SYSTEMS`) is untouched; a second, explicit mechanism is added alongside it rather than trying to bend the first one to fit.
2. **Homeostat is periodic; books are one-and-done.** A book's `cleanup` task, once `done`, should never silently re-run — that's the entire point of the ADR 004 ledger. A monthly homeostat is the opposite: every invocation should redo the whole chain, producing a fresh dated snapshot, regardless of what last month's ledger entry says. Retrofitting "done forever" semantics with a notion of "done for this period" would mean date-aware ledger keys and a cadence concept neither ADR 004 nor ADR 005 has — real, avoidable complexity for a chain that has no fan-out and no reason to ever skip a step.

**The actual mechanism, given both of those:** a small, separate, *simpler* runner in `lib/orchestrator.py` — not a generalization of `run_book()`, a parallel function for exactly this shape (linear, periodic, always re-executed):

```python
# lib/orchestrator.py — additions alongside the existing book-scoped functions,
# which are entirely unchanged.

HOMEOSTAT_TASKS = [("s5", "homeostat-scan"), ("s5", "homeostat"), ("s5", "homeostat-render")]


def run_homeostat(root: Path, config: dict, step: bool = False) -> dict:
    """Run System 5's homeostat chain in declared order, unconditionally —
    periodic, not one-and-done, so there is no readiness/"already done" check
    here at all, unlike run_book(). A failure stops the chain immediately:
    this is strictly linear (no independent siblings the way S1D's fan-out
    tasks are), so a broken `homeostat-scan` must not let `homeostat` run
    against stale or missing input.
    """
    done, failed = [], []
    input_file = None
    for system, name in HOMEOSTAT_TASKS:
        task = _task_dict(system, name)
        engine = importlib.import_module(f"engines.{task['engine']}")
        label = f"{system}.{name}"
        click.echo(f"[s2] Running {label}...")
        try:
            input_file = task_loader.run_task(
                root, config, system, task,
                input_file=input_file if getattr(engine, "CLI_ARG", "file") != "none" else None,
            )
            done.append(label)
        except Exception as e:
            click.echo(f"[s2] {label} failed: {e}")
            failed.append(label)
            break  # linear chain — no point running the next stage against a failure
        if step:
            break
    return {"done": done, "failed": failed, "complete": len(done) == len(HOMEOSTAT_TASKS)}


def homeostat_status(root: Path) -> list:
    """Read-only: each homeostat task's last recorded outcome, straight from
    the ledger — no readiness computation, since there's no notion of
    "blocked" in a chain that's always re-run top to bottom.
    """
    ledger = manifest.load(root).get("tasks", {})
    return [{"system": s, "name": n, **ledger.get(f"{s}.{n}", {"status": "never run"})}
            for s, n in HOMEOSTAT_TASKS]
```

`task_loader.run_task()` still records every step to the ledger through the same shared hook every other task uses (ADR 004 point 4) — so `homeostat_status()` can show "last run: 2026-08-01, done" per stage even though nothing here gates on it. The ledger becomes a history log for this chain, not a readiness gate.

**CLI**, nested under the existing `s2` group rather than bolted onto `s2 status`/`s2 run` (which take a `book_slug` and mean something specific — a singleton, no-slug project deserves its own subcommand, not an optional argument overloading what `s2 status`/`s2 run` already mean):

```
pipeline.py s2 homeostat status
pipeline.py s2 homeostat run [--step]
```

This is the whole reduction the review asked for: **two commands** (`s2 homeostat run`, and separately `s5 log-decision` for the one genuinely human step) replace three task commands plus manual sequencing — `homeostat-scan`/`homeostat`/`homeostat-render` stop being commands a person needs to remember exist at all; they're implementation detail of `s2 homeostat run` now, the same way `s1b`'s five tasks are implementation detail of `s2 run <book_slug>`.

A generic "project registry" (formalizing "book" and "homeostat" as two named, pluggable project types inside `orchestrator.py`, so a third one later doesn't need its own bespoke function) was considered and deliberately not built — see Alternatives. `HOMEOSTAT_TASKS` being a plain module-level list, not a registry entry, is the deliberately smaller version of that idea.

---

## Alternatives Considered

- **A general-purpose Markdown-to-HTML library** (e.g. `markdown`, `mistune`) — considered, rejected for now: every prompt in this project produces a known, narrow subset (headings, paragraphs, bullets) since that's what fill-in-the-template prompts are designed to produce; a bespoke converter for exactly that subset is smaller and has no surface area for formatting this project will never generate. Revisit if a future prompt genuinely needs tables or nested lists.
- **A JS charting library (even bundled, no CDN)** — rejected: one bar chart per book is well within what inline SVG generated by Python can do directly; adding a JS dependency for this is disproportionate and reintroduces a browser-compatibility surface this project doesn't need.
- **An on-demand local web server with a real decision-logging form** (Option 3 from the original discussion) — rejected for this ADR: more capable, but the first thing in this project resembling a running process rather than a one-shot command. Worth doing deliberately later if the CLI-based log ever feels like a real friction point, not by default now.
- **Leaving `homeostat-scan`/`homeostat`/`homeostat-render` as three hand-sequenced commands, matching System 4's precedent** — this was the first draft's decision, revised after review: three commands to remember and sequence in order is precisely the class of problem System 2 exists to remove (ADR 004's own framing — "turning thirteen hand-typed commands into one"), not a reason to exclude something from it. Superseded by point 7.
- **Generalizing `run_book()`/`_load_graph()` to cover the homeostat chain directly** (a single unified orchestrator for both books and homeostat) — considered, rejected: the two have genuinely different semantics (one-and-done with fan-out vs. periodic and strictly linear), and bending one model to cover both would mean threading a "periodic, no readiness gate" special case through code that today has neither concept. A second, small, purpose-built function (point 7) is more honest about the difference than a shared abstraction stretched to cover it.
- **A formal, pluggable "project registry"** (so a third non-book periodic pipeline later doesn't need its own bespoke orchestrator function) — considered, deferred: exactly one non-book periodic project exists right now. Building a registry abstraction for a second instance that doesn't exist yet is the premature-generalization mistake ADR 004 point 7 already declined to make for System 4. Revisit if and when a second one actually shows up.
- **Folding next-steps.md item 5 (S5 evaluate's verdict-calibration log) into this ADR** — rejected: different log, different task, thematically adjacent but not the same piece of work. Keeping scope to item 1 only, as discussed.
- **An interactive, editable decision file (like a candidate brief) instead of two CLI arguments** — rejected: decision-log entries are meant to be short (a sentence or two each), not discursive documents; a free-text file to open, edit, and save is more ceremony than the content warrants.

---

## Consequences

**Easier:**
- The Editorial Director gets one artifact per period that actually juxtaposes "what's it costing us" against "what's changing out there," instead of mentally cross-referencing two separate outputs.
- Zero new runtime dependencies — everything reuses `llm_text.py`, the existing `<fuente>`-tag convention, `lib/dashboard.py`'s already-structured data, and stdlib-only HTML generation.
- Decision history accumulates automatically in a form later useful for the still-open "graduation criteria" question the original assessment named — the same evidence-building idea next-steps.md item 5 proposes for candidate verdicts, now also happening here.
- **Down to two commands to remember for the whole homeostat cycle** (`s2 homeostat run`, `s5 log-decision`) instead of three task commands plus manual sequencing — the exact reduction System 2 already gives book production, now extended here.

**Harder / needs care:**
- `lib/orchestrator.py` now has two genuinely different execution models side by side (`run_book()`'s readiness/ledger-gated graph, `run_homeostat()`'s unconditional linear re-run) — worth a comment at the top of the file pointing each reader to which one applies where, so a future contributor doesn't assume they're interchangeable.
- `run_homeostat()` never checks "already done" — running `s2 homeostat run` twice in the same day produces two dated snapshots, not an error and not a no-op. This is correct for a periodic artifact but is a real behavioral difference from `s2 run <book_slug>` worth knowing before typing it out of habit.
- The bespoke Markdown-subset converter is only as good as the narrow assumption it's built on (headings/paragraphs/bullets only) — a future prompt change that introduces different formatting (e.g. a table) will render incorrectly until the converter is extended; this is a real, accepted constraint, not a hidden one.
- The homeostat narrative depends on `s4 briefing` already having been run recently — an empty or stale S4 briefing produces a thinner "tensions" section with less to say, silently, not as an error. Worth remembering to run `s4 scan`/`s4 briefing` first in practice (this ADR doesn't chain into System 4's own commands — `homeostat-scan` reads whatever S4 briefing already exists on disk, it doesn't trigger a fresh one).
- This still leaves `vsm.md`'s System 5 only partially implemented in one sense — the arbitration is *surfaced*, but nothing enforces that a homeostat page actually gets read or acted on monthly. Same limit every advisory artifact in this project already has.

---

## Implementation Checklist

- [ ] Add `homeostat_dir: homeostat` to `config.example.yaml` (and the user's real `config.yaml`), mirroring `intelligence_dir`/`candidates_dir`
- [ ] Add `paths.homeostat_root(config)` to `lib/paths.py`, mirroring `intelligence_root()`/`candidates_root()`
- [ ] Create `engines/homeostat_scan.py` (`CLI_ARG = "none"`): read `lib.dashboard.portfolio_summary()`, the latest `intelligence/s4/briefing/<date>/combined.txt`, and `homeostat/decisions.yaml`; write a dated `combined.txt` using `feed_scan._wrap_source()`-style `<fuente>` tags (or a locally duplicated equivalent)
- [ ] Create `prompts/s5/homeostat_task.txt` (real, Spanish) and `prompts/examples/s5/homeostat_task.txt` (English reference) — fill-in-template per point 6
- [ ] Create `engines/homeostat_render.py` (code only, no LLM call): reads the `homeostat` task's narrative output plus fresh S3/S4/decision data; renders a self-contained `homeostat.html` (inline CSS, inline SVG bar chart, bespoke Markdown-subset-to-HTML for the two embedded documents, a rendered decision-log table)
- [ ] Add `homeostat-scan`, `homeostat`, `homeostat-render` to `systems/s5/tasks.yaml`
- [ ] Add `pipeline.py s5 log-decision "<tension>" "<decision>"` (hand-written, appended to the `s5` group after `build_system_group()` returns it) — appends one entry to `homeostat/decisions.yaml` with today's date
- [ ] Add `HOMEOSTAT_TASKS`, `run_homeostat()`, and `homeostat_status()` to `lib/orchestrator.py` (point 7) — additive, `run_book()`/`_load_graph()`/`BOOK_SYSTEMS` untouched
- [ ] Add a `homeostat` subgroup under `s2` in `pipeline.py`: `s2 homeostat status`, `s2 homeostat run [--step]`
- [ ] Add `/homeostat/` to `.gitignore`, alongside `/books/`, `/intelligence/`, `/candidates/`
- [ ] End-to-end test: `s2 homeostat run` against real S3/S4 data (or realistic disposable fixtures if real data is thin) drives all three stages correctly in order; confirm a deliberate failure in `homeostat-scan` stops `homeostat`/`homeostat-render` from running; confirm running `s2 homeostat run` a second time produces a fresh dated snapshot rather than a no-op; log a test decision and confirm `s2 homeostat status` and a re-run of `homeostat-render` alone both reflect it without re-running the LLM step; open the resulting HTML in a browser and confirm it renders correctly with no network access
- [ ] Update README with the System 5 homeostat section, the `s2 homeostat` commands, and the command reference table
