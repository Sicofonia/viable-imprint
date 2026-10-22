# ADR 023 — Terminal UI (Textual) Over the Existing Pipeline

**Status:** Approved. Written as an ordered task list for Claude Code; every task states its scope, its tests, and when it is done.

---

## Context

The pipeline now exposes about forty commands across every VSM system: `init`, `book`, `candidate`, `s1b`, `s1c`, `s1d`, `s2` (with `homeostat` and `newsletter` subgroups), `s3`, `s4`, `s5`. The Editorial Director does not run them daily, so they are never memorised. `docs/cheat_sheet.txt` was written to compensate, but it is hand-maintained and drifts whenever a task or option is added, renamed or removed.

Three front ends were assessed (local web app, packaged desktop app, terminal UI). A terminal UI built with [Textual](https://textual.textualize.io/) was chosen:

- One new Python dependency, no new system requirement (no server, browser, per-OS packaging or signing). Python, `uv` and LibreOffice stay required as before.
- Cross-platform wherever the pipeline already runs (the operator uses Windows).
- Its menus are generated from the pipeline itself, so they cannot drift the way the cheat sheet does.
- Visual outputs (the homeostat's `homeostat.html` with its SVG cost chart, finished deliverables, PDFs) open in the operating system's default application instead of being drawn in the terminal.
- `textual serve` can later show the same app in a browser without a rewrite. Not in scope.

This ADR relaxes the project's "command line only" stance. It does **not** relax "no background process": the UI is a foreground program the operator opens and closes, and it never runs anything on its own.

### Facts checked in the code before writing this ADR

- `pipeline.py` already has a global `--config PATH` option.
- The full CLI is one Click tree (`pipeline.cli`). Manifest-driven groups (`s1b`, `s1d`, `s4`, `s5`, plus `s3 sales-ingest`) are built from `systems/*/tasks.yaml`; the rest are hand-written. Engines add per-task options through `CLI_OPTIONS` (e.g. `s1b translate --document`, `s1b format --template`).
- `lib/task_loader.load_system_tasks()` reads `systems/<name>/tasks.yaml` relative to the current directory, and prompt, template and `sources_config` paths in the manifests are repository-relative too. Anything that imports `lib/` must run with the repository root as its working directory.
- Existing tests (`tests/test_*.py`) use `unittest`, stub `httpx.post`/`time.sleep`, and are run with `uv run python -m unittest discover -s tests`. No pytest.
- The run-state ledger is written only through `lib/task_loader._run_recorded()` (`manifest.record_task` / `mark_stale`). `manifest.update()` refuses `tasks` and `sales`. `record_task` is a plain load-modify-save with no file locking.
- `_run_recorded()` catches `BaseException`, so a `KeyboardInterrupt` during a task is recorded in the ledger as `failed` before re-raising. A hard kill records nothing.
- Providers are chosen in `providers/__init__.py` (`get_llm_provider`, `get_translation_provider`) from `config["llm"]["provider"]` and `config["translation"]["provider"]`.
- The configured defaults are Z.ai `glm-4.7-flash` (priced at 0) and eTranslation (free, asynchronous, polls for up to 120 minutes, can resume an interrupted request). Google AI Studio's free tier, if switched back, allows about 20 requests per day.
- Periodic chains are run by `s2 homeostat run` and `s2 newsletter run`. Their individual stages (`s5 homeostat-scan`, `s1d newsletter-scan`) also appear as direct commands, but the generic no-input-file callback in `task_loader._build_command` always resolves its root with `paths.intelligence_root()`, so running those two directly writes into `intelligence/` instead of `homeostat/` or `newsletter/`. To be confirmed in T0.1; the UI never runs them directly either way.
- The dashboard formatting helpers (`_format_cost`, `_format_duration`, `_format_span`, `_format_revenue`, `_format_margin`) are private functions inside `pipeline.py`.

---

## Decision

### 1. Same repository, isolated package, optional dependency

The UI lives in this repository as a self-contained `tui/` package. Textual is an optional extra (`uv sync --extra tui`). A new `pipeline.py ui` command imports `tui` lazily; without Textual it prints the install command and exits non-zero. The pipeline keeps working, unchanged, without Textual.

**Removal procedure** (every task keeps this true): delete `tui/` and `tests/tui/`, remove the `ui` command from `pipeline.py`, remove the `tui` extra from `pyproject.toml`. No module outside `tui/` and `tests/tui/` may import `tui`. The fake providers, the test harness and `lib/formatting.py` from Phase 0 stay, because they are useful without the UI.

### 2. Read in-process, change state only through the CLI

- **Reading** calls existing `lib/` functions directly: `orchestrator.book_status` / `periodic_status`, `dashboard.book_summary` / `portfolio_summary` / `deviation_flags`, `homeostat.load_decisions` / `latest_s4_briefing`, `policy_check.current_hash` / `stored_hash`, `manifest.load`. No logic is re-implemented. A changed signature breaks a UI test immediately.
- **Every action that runs a task or changes pipeline state** runs `pipeline.py` as a subprocess, with exactly the arguments the operator would type. This keeps one execution path, so ledger recording, metrics and stale-cascading (ADRs 004, 005, 009) behave identically from the terminal or the UI. It also keeps the UI responsive during long runs (eTranslation can take an hour or more), lets output stream line by line, and isolates the UI from engine crashes.
- The UI never writes `manifest.yaml`, any `tasks.yaml`, prompts, templates or config. The only files it writes are operator inputs a human would otherwise create by hand: copying a chosen manuscript into a new book's `s1b/source/`, creating an empty candidate brief, and creating this month's newsletter notes file (`newsletter/notes/YYYY-MM.txt`).
- **One run at a time.** Because the ledger has no file locking, the UI refuses to start a second subprocess while one is running. (It cannot stop the operator running the CLI in another terminal at the same time; the help screen says so.)

### 3. Two layers: a generic command explorer, plus screens for recurring work

- **Command explorer.** Built by introspecting the Click tree passed in from `pipeline.py ui`: every group and command with its help text, every argument and option rendered as a form field (file picker for `click.Path`, checkbox for flags, dropdown for `click.Choice`, number field for `float`/`int`). This guarantees that every current and future command, in every system, is reachable from the UI with no UI change. It is what replaces the cheat sheet.
- **Workflow screens.** For the recurring flows, screens read state and pre-fill arguments, so the operator picks a book or a candidate instead of typing paths: book production (S1B/S1D/S2), print prep (S1C), dashboard and sales (S3), intelligence and outbound articles (S4 and S1D `article-draft`), candidates and policy (S5), periodic pipelines (homeostat, newsletter), and environment health.

System-specific code in `tui/` is limited to these workflow screens and to the facts that the hand-written commands encode (folder conventions from ADRs 003, 006, 007, 008, 011). Task lists always come from the manifests or the Click tree.

### 4. Every action shows its command-line equivalent

Every action displays the exact `uv run python pipeline.py ...` command before it runs, and the run log repeats it. The UI teaches the CLI instead of hiding it.

### 5. Confirmation before provider calls

Before running anything whose engine calls an LLM or translation provider (`llm_text`, `translation`), or a chain containing one, the UI shows: provider and model; the input size in characters; an approximate number of provider calls (using the same chunk-size resolution the engine uses, `engines.llm_text._resolve_default_max_chars` and the task's `max_chars`); and an estimated cost from the configured `pricing:` block, or "no pricing configured". Call counts are shown because, on free tiers, the daily request quota is the scarce resource, not money. Zero-provider actions (`odt_format`, `metadata_doc`, `feed_scan`, `sales_ingest`, `s1c`, dashboards) run without confirmation.

### 6. Cancelling a run

Cancel first sends an interrupt the child Python process sees as `KeyboardInterrupt` (SIGINT on macOS/Linux; on Windows the child is started in a new process group and sent `CTRL_BREAK_EVENT`), waits up to 10 seconds, then terminates. **Windows needs child-side help, confirmed in T0.1:** `CTRL_BREAK_EVENT` alone kills the child without any Python exception, and `signal.signal(SIGBREAK, ...)` alone does not interrupt a blocking `time.sleep()`. The runner therefore sets `VIABLE_IMPRINT_GRACEFUL_BREAK=1` in the child's environment, and `pipeline.py`'s `__main__` block calls `lib/interrupt.install_graceful_break()` when that variable is set; it registers a console control handler that raises `KeyboardInterrupt` in the main thread, the same as Ctrl+C would (see Verification notes, item 3). With a graceful interrupt, `_run_recorded()` records the task as `failed`, which is already retry-eligible. After a forced kill, the ledger is unchanged and the UI says so. For an eTranslation run, the confirmation dialog notes that re-running resumes the in-flight request (ADR 022, Decision 6).

### 7. Outputs open externally

Files and folders open with `click.launch()` (already a dependency; works on Windows, macOS and Linux). The UI does not render deliverables.

### 8. Testing costs nothing by default

All automated tests run against **fake providers** (no network, no key, no quota). A separate **live smoke script** runs real providers against a single tiny paragraph, only when the operator runs it explicitly, with a call-count preview, a confirmation, and a cost ceiling.

### Out of scope

Editing `tasks.yaml`, prompts, templates or config from the UI; previewing deliverables inside the UI; scheduling; network or multi-user access; fixing pre-existing pipeline bugs found along the way (they are recorded in the implementation notes, as in earlier ADRs, unless they block a UI task); building System 1A.

---

## Skeleton

```
tui/
├── __init__.py
├── app.py                 # PipelineApp: screen routing, key bindings, command palette
├── app.tcss               # stylesheet
├── services/              # plain Python, no Textual imports, unit-testable
│   ├── context.py         # repo root, config path + loaded config, chdir guard
│   ├── command_model.py   # Click tree -> CommandSpec/ParamSpec (explorer + argv)
│   ├── commands.py        # the ONLY place that builds pipeline.py argv lists
│   ├── runner.py          # async subprocess: stream, exit code, graceful cancel, one-at-a-time lock
│   ├── estimate.py        # provider/model, input chars, approx calls, approx cost (Decision 5)
│   ├── state.py           # thin wrappers over lib.orchestrator / dashboard / homeostat / policy_check
│   ├── files.py           # open_path, newest dated folder, copy source, operator input files
│   └── health.py          # environment checks (Phase 8)
├── screens/
│   ├── home.py            # entry: books + shortcuts to every screen
│   ├── explorer.py        # generic command explorer (all commands)
│   ├── book.py            # S1B/S1D/S2 for one book
│   ├── run.py             # modal: confirmation, streamed log, cancel, summary
│   ├── print_prep.py      # S1C
│   ├── performance.py     # S3 dashboard + sales ingest + ISBN
│   ├── intelligence.py    # S4 scan/briefing/content-strategy + S1D article-draft
│   ├── periodic.py        # S2 homeostat + S2 newsletter (+ S5 decision log)
│   ├── candidates.py      # S5 evaluate, calibration, policy sync
│   └── health.py
└── widgets/
    ├── command_hint.py    # "Equivalent command: uv run python pipeline.py ..."
    └── param_form.py      # renders a CommandSpec as a form

providers/llm/fake.py           # Phase 0 (pipeline side)
providers/translation/fake.py   # Phase 0 (pipeline side)
lib/formatting.py               # Phase 0: dashboard formatters moved out of pipeline.py

tests/
├── harness.py             # shared workspace/config/book helpers (see T0.3)
├── fixtures/
│   ├── tiny_source.txt          # the test paragraph (below)
│   ├── tiny_candidate_brief.txt
│   ├── tiny_feed.xml            # 2-item RSS feed, served through a stubbed httpx.get
│   ├── tiny_sources.yaml        # one feed, no Gutenberg, no catalog pages
│   ├── tiny_combined_scan.txt   # pre-built s4 scan output for s4 briefing
│   ├── tiny_marketing_metadata.yaml
│   ├── tiny_ingramspark.csv     # headers matching providers/sales/ingramspark.py
│   ├── tiny_kdp.csv             # headers matching providers/sales/kdp.py
│   └── verdict_sample.txt       # a real-shaped s5 verdict with a <verdict> tag
├── test_fake_providers.py
├── test_pipeline_baseline.py    # Phase 0 regression baseline
├── tui/                         # skipped automatically when Textual is not installed
│   └── test_*.py
└── live/
    └── smoke.py                 # opt-in script, real providers, tiny inputs only
```

### Key interfaces

```python
# tui/services/command_model.py
@dataclass
class ParamSpec:
    name: str              # Click parameter name
    kind: str              # "argument" | "option" | "flag"
    type: str              # "path" | "choice" | "float" | "int" | "text"
    required: bool
    default: object
    choices: list[str] | None
    help: str | None
    opts: list[str]        # e.g. ["--trim-w"]

@dataclass
class CommandSpec:
    path: list[str]        # e.g. ["s2", "homeostat", "run"]
    help: str
    params: list[ParamSpec]

def build_model(root: click.Group) -> list[CommandSpec]: ...   # every leaf command

# tui/services/commands.py — pure functions
def base_argv(ctx: Context) -> list[str]:          # [sys.executable, "<repo>/pipeline.py", "--config", "<path>"]
def argv_for(ctx, spec: CommandSpec, values: dict) -> list[str]: ...
def display(argv: list[str]) -> str: ...           # "uv run python pipeline.py ..." with quoting

# tui/services/runner.py
@dataclass
class RunResult:
    argv: list[str]
    returncode: int | None
    cancelled: bool
    forced: bool           # True if terminate was needed after the graceful interrupt
    lines: list[str]

class TaskRunner:
    busy: bool
    async def run(self, argv: list[str], on_line: Callable[[str], None]) -> RunResult: ...
    async def cancel(self, grace_seconds: float = 10) -> None: ...
```

`pipeline.py ui` passes its own root Click group and the `--config` value into the app, so the UI never imports `pipeline.py` itself. Subprocesses run with `sys.executable` (the `uv`-managed interpreter that launched the UI), working directory set to the repository root, and `PYTHONUNBUFFERED=1` and `PYTHONIOENCODING=utf-8` so output streams immediately and Spanish characters survive on Windows.

### Test paragraph (`tests/fixtures/tiny_source.txt`)

Original text written for this harness, in period style, with deliberate OCR artefacts (`carnels`, a broken hyphenation, a stray page number and running header) so `cleanup` has real work to do. About 520 characters, so each LLM task is one call.

```
CHAPTER I.
THE ROAD TO KASHGAR.

On the morning of the 14th we left the caravanserai before dawn, the carnels
already loaded and grumbling in the cold. The track followed the river for
some hours, then turned sharply into the hills, where the snow lay thin upon
the stones. Our guide, a taciturn Kirghiz, assured me that the pass would be
open, though he said so with- out much conviction.

12

THROUGH THE HIGH PAMIR

By noon the wind had risen, and we halted beneath an overhang to brew tea.
```

---

## Test tiers

| Tier | How it runs | Providers | Network | Runs by default |
|---|---|---|---|---|
| Unit | in-process | none or fake | no | yes |
| Pipeline in-process | `run_task()` with task dicts pointed at `prompts/examples/` and fixture configs | fake | no | yes |
| Pipeline CLI | `pipeline.py` subprocess with a test `--config` | fake | no | yes; skips a case when a personal file it needs is missing |
| TUI | Textual `App.run_test()` / Pilot, headless, in `unittest.IsolatedAsyncioTestCase` | fake | no | yes, if Textual is installed |
| LibreOffice | `s1b format` | none | no | skips if LibreOffice or the configured template is missing |
| Live smoke | `uv run python tests/live/smoke.py` | real | yes | **never** — explicit command plus confirmation |

Rules for every automated test:

- Each test gets its own temporary `books_dir`, `intelligence_dir`, `candidates_dir`, `homeostat_dir` and `newsletter_dir`, written as absolute paths into a generated test config. No test reads or writes the operator's real data folders; a guard test compares their listings before and after the suite.
- Tests run with the repository root as working directory (manifest, prompt and template paths are repository-relative).
- Personal, gitignored files (`prompts/s1b|s1d|s4/*_task.txt`, `templates/marketing_metadata.yaml`, `templates/format_styles.yaml`, `systems/s4/sources.yaml`) are never created or copied by tests. In-process tests point task dicts at `prompts/examples/` and at fixtures instead. CLI tests that need a personal file skip with a message naming it. On the operator's machine everything runs; on a clean clone the in-process tier still covers every engine.
- Fake providers are selected by config only (`provider: fake`). Network-touching code paths that do not go through a provider (`feed_scan`'s `httpx.get`) are stubbed in-process, following the existing tests' style.

---

## Implementation tasks

Phases are in order; each depends on the previous one. Within a phase, tasks are in order.

### Phase 0 — Groundwork (pipeline side, minimal)

#### T0.1 — Verify the assumptions

**Scope:** Read the code and record findings under "Verification notes" at the end of this ADR. No code changes. Confirm or correct each item in "Facts checked in the code", plus:

1. Whether importing `pipeline` has side effects beyond `load_dotenv()` and registering commands (the UI receives the Click tree from the `ui` command, but tests will build it too).
2. The direct-invocation root problem for `s5 homeostat-scan` and `s1d newsletter-scan` (record only; do not fix here).
3. How a child Python process on Windows reacts to `CTRL_BREAK_EVENT` when started with `CREATE_NEW_PROCESS_GROUP`: does `_run_recorded()` see a `KeyboardInterrupt`? If not, record what the runner must do instead (for example, an environment variable the UI sets and a one-line `signal.signal(signal.SIGBREAK, signal.default_int_handler)` in `pipeline.py`'s `__main__` block, applied only when that variable is set). Update Decision 6 with the result.
4. The exact header rows `providers/sales/ingramspark.py` and `kdp.py` match, for the CSV fixtures.
5. Whether `uv sync --extra tui` works with the current `pyproject.toml` and lock file layout.

**Tests:** none (research).

**Done when:** every item has a recorded answer, and any task below that depends on a corrected answer has been updated.

#### T0.2 — Fake providers

**Scope:** Add `providers/llm/fake.py` (`FakeLLMProvider(LLMProvider)`) and `providers/translation/fake.py` (`FakeTranslationProvider(TranslationProvider)`), selected with `provider: fake` in `get_llm_provider` / `get_translation_provider`. No API key required.

- `complete()` returns deterministic Spanish-looking text that satisfies every structural contract code parses downstream: an `<overview>…</overview>` block (read by `metadata_doc`), `<explorer>…</explorer>` and `<dish>…</dish>` (read by `newsletter_track`), a `<verdict>…</verdict>` line (read by `candidate record-decision`), at least one `## ` heading, and the first 200 characters of the input. It accumulates `usage` like the real providers (tokens estimated as characters ÷ 4).
- `translate()` returns its input unchanged, preserving `[i]…[/i]`, `[sc]…[/sc]` and `[FN: …]`, and accumulates `usage["characters"]`. It declares no `translate_document`, `begin_run` or `finish_run`.
- Config keys under `llm:` / `translation:`: `fail: true` makes every call raise; `delay_seconds: N` sleeps before returning (for cancel tests).
- `_resolve_default_max_chars` falls back to its existing default for `fake`.

**Tests (`tests/test_fake_providers.py`):**
- Same input gives the same output; every structural tag above is present.
- `usage` accumulates over several calls; `lib.metrics.enrich` turns it into ledger fields, with `cost_usd` computed from a test `pricing:` block.
- Markup is preserved by the fake translator.
- `fail: true` raises; `delay_seconds` delays.
- No network: `httpx.post`, `httpx.get` and `deepl.Translator` patched to raise if touched.
- Selecting `fake` needs no environment variable.

**Done when:** tests pass; README notes the fakes exist for tests only.

#### T0.3 — Test harness

**Scope:** `tests/harness.py` (plain helpers; `unittest`, no new test dependency) and the fixture files in the Skeleton.

- `Workspace` context manager: creates temporary data folders, writes a test config (`llm.provider: fake`, `llm.model: fake-1`, `translation.provider: fake`, a fixed `pricing:` block, an `s3.deviation` block, all five `*_dir` keys as absolute paths), and removes everything afterwards.
- `make_book(ws, slug)`: runs `init` through the CLI, then copies `tiny_source.txt` into `s1b/source/`.
- `run_cli(ws, *args) -> (returncode, output)`: runs `pipeline.py --config <test config> ...` as a subprocess from the repository root.
- `example_task(system, name, **overrides)`: the task dict from `tasks.yaml` with `prompt` / `reduce_prompt` redirected to `prompts/examples/` and `metadata_config` / `sources_config` redirected to fixtures.
- `require_personal_files(system, name)`: skip with the missing file's path.
- `RealDataGuard`: records listings of the operator's real data folders and asserts they are unchanged.

**Tests:**
- `run_cli(ws, "s1b", "cleanup", <source>)` with fakes exits 0 when the personal cleanup prompt exists (skips otherwise); the ledger has `s1b.cleanup` done with `duration_seconds`, `usage`, `cost_usd`.
- `run_task()` with `example_task("s1b", "cleanup")` succeeds in-process on a clean clone.
- `RealDataGuard` passes after both.

**Done when:** `uv run python -m unittest discover -s tests` passes on a clean clone and on the operator's machine, with skips listing their reasons.

#### T0.4 — Pipeline regression baseline (fake providers)

**Scope:** `tests/test_pipeline_baseline.py`. Exercise every system before any UI exists. This is the net that proves later work changed nothing, and it validates the argument shapes the UI will build. Each case runs in-process with `example_task()` unless it is about the CLI itself.

- **S1B:** `cleanup → translate → ortho → copyedit` produce files under `s1b/…/es/`; `format` (LibreOffice tier) produces an `.odt`.
- **S1D book tasks:** `brief` from the copyedit output; the six expansion tasks and `metadata` from the brief; `metadata.md` contains the fake `<overview>` text and the fixture facts; `press-dossier` ends with the fixture contact block.
- **S2 book orchestration (CLI):** `s2 status` on a fresh book (cleanup ready, rest blocked); `s2 run --step` runs one task; `s2 run --only s1b` completes S1B except `format` without LibreOffice; re-running `s1b cleanup` marks downstream tasks `stale` (ADR 009); `fail: true` gives a recorded `failed`, independent siblings still run, exit code non-zero, next run retries.
- **S2 periodic (CLI):** `s2 homeostat run` writes `homeostat.html` under the test `homeostat_dir`; `s2 newsletter run` with a notes file for the current month writes the final newsletter and records the fake explorer and dish; a second run warns about the repeat; `s2 homeostat status` / `s2 newsletter status` list all three stages.
- **S3:** `book set-isbn`, then `s3 sales-ingest` with each CSV fixture (auto-detected) and with `--format`; ingesting the same CSV twice adds nothing (ADR 011 dedupe); `s3 dashboard` and `s3 dashboard <slug>` show revenue, cost and a deviation flag for a fixture portfolio built to trigger one.
- **S4:** `scan` with `tiny_sources.yaml` and a stubbed `httpx.get` returning `tiny_feed.xml` writes `combined.txt`; a second scan reports nothing new; `briefing` from `tiny_combined_scan.txt`; `content-strategy` from the briefing; S1D `article-draft` from the content-strategy output.
- **S5:** `candidate new`; `s5 evaluate` on `tiny_candidate_brief.txt`; `candidate record-decision` writes `calibration.yaml` with the fake verdict; `s5 log-decision` appends to `decisions.yaml`; `s5 check-policy-sync` exits 0 on the committed files (read-only; `--update` is never run against the real prompt).
- **S1C:** `s1c check`, `set-interior`, `set-cover` on PDFs generated in the test with `pikepdf` (blank pages of known millimetre sizes); the resulting TrimBox/BleedBox match the cheat sheet's worked example (304 + 12 + 2×3 = 322 × 235 mm cover).

**Done when:** all pass or skip with named reasons, with no network.

#### T0.5 — Move dashboard formatters into `lib/formatting.py`

**Scope:** Move `_format_cost`, `_format_duration`, `_format_span`, `_format_revenue`, `_format_margin` from `pipeline.py` into `lib/formatting.py` as public functions; `pipeline.py` imports them. Pure move, no behaviour change, so the UI shows numbers exactly as the CLI does.

**Tests:** before the move, capture `s3 dashboard` and `s3 dashboard <slug>` output for the T0.4 fixture portfolio; after the move, the output is byte-identical. Unit tests for each formatter, including sub-cent costs (ADR 005 point 9), `None`, negative margins and multi-currency revenue.

**Done when:** tests pass.

### Phase 1 — UI foundation

#### T1.1 — Package, optional extra, entry point

**Scope:** Create `tui/` per the Skeleton. Add Textual as optional extra `tui` in `pyproject.toml`. Add `pipeline.py ui`: imports `tui` lazily, prints `Install the UI with: uv sync --extra tui` and exits non-zero if Textual is missing; otherwise starts the app with the root Click group and the `--config` path. `services/context.py` changes the working directory to the repository root if needed. Minimal app: header, footer, empty home screen, `q` to quit.

**Tests:**
- With `textual` import made to fail (patched), `pipeline.py --help` and an existing command still work, and `ui` prints the install hint.
- `App.run_test()` starts and quits with `q`.
- No module outside `tui/` and `tests/tui/` imports `tui` (scan of import statements).
- `services/` contains no Textual import.

**Done when:** tests pass; README gains a short "Using the UI" section (install, launch).

#### T1.2 — Command model and argv builder

**Scope:** `command_model.build_model()` walks the Click tree and returns one `CommandSpec` per leaf command, including nested groups (`s2 homeostat run`) and engine `CLI_OPTIONS`. `commands.argv_for()` turns a spec plus form values into an argv list; `display()` renders it as a copy-pasteable `uv run python pipeline.py …` line. No other module builds a `pipeline.py` command line.

**Tests:**
- Built against the real `pipeline.cli`: the set of command paths equals the set Click itself reports (recursive walk in the test); `s1b translate` has a `--document` flag; `s1b format` has `--template`; `s1c set-cover` has `--trim-w`, `--trim-h`, `--spine-w`, `--bleed`, `--unit` with correct defaults and `mm|pt` choices; `s2 run` has `--only` with choices `s1b|s1d`.
- A temporary `tasks.yaml` with an extra task (via a test-only group built with `build_system_group`) shows up in the model.
- `argv_for` produces exactly what a hand-typed command would, for representative commands, including paths with spaces and non-ASCII characters; required parameters missing raise a clear error.
- Running a built argv for `s2 status <slug>` through `run_cli`'s subprocess path gives the same output as the hand-typed command.

**Done when:** tests pass.

#### T1.3 — Runner

**Scope:** `TaskRunner` per Key interfaces: `asyncio.create_subprocess_exec`, stdout and stderr merged, `on_line` called per line as it arrives, graceful cancel then terminate per Decision 6 (using what T0.1 found for Windows), and a `busy` flag that makes a second `run()` raise.

**Tests (against small `python -c` scripts first):**
- Lines arrive in order and before the process exits; non-zero exit codes returned; non-ASCII lines intact.
- Cancel of a sleeping script returns `cancelled=True`; a script that ignores the interrupt is terminated and returns `forced=True`.
- A second `run()` while busy raises.
- Integration with fakes: `s1b cleanup` on a harness book through the runner records `done`; with `delay_seconds: 5` and a cancel after 1 second, the ledger records `failed` with an interrupt error (on Windows only if T0.1 confirmed the mechanism; otherwise the test asserts the ledger is unchanged and the gap is recorded).

**Done when:** tests pass on the operator's OS.

#### T1.4 — App shell and navigation

**Scope:** Home screen with entries for every screen in the Skeleton; key bindings shown in the footer; `?` help panel listing bindings and the "one run at a time / don't run the CLI in parallel" note; `command_hint` widget on every action screen; the run modal (`screens/run.py`) used by every action: confirmation (Decision 5, via `services/estimate.py`), streamed log, cancel button, final summary, "open output" when the command printed a `Saved:` path.

**Tests (Pilot):**
- Every screen opens by key binding and from the command palette; `escape` returns home.
- The run modal streams a fake run, shows the summary, and disables "run" buttons everywhere while busy.
- `estimate.py`: for a fixture input and the fake provider, call count and cost match a hand calculation; "no pricing configured" shown when `pricing:` is absent; no confirmation for `metadata_doc`.

**Done when:** tests pass.

#### T1.5 — Command explorer

**Scope:** `screens/explorer.py`: a filterable tree of every command (grouped by top-level group, labelled with the group's help text), a detail panel with the command's help, a form from `widgets/param_form.py`, the command hint, and Run (through the run modal). Commands not meant to be run directly are still listed, with a warning instead of a Run button when T0.1 confirmed a problem: `s5 homeostat-scan`, `s5 homeostat`, `s5 homeostat-render`, `s1d newsletter-scan`, `s1d newsletter`, `s1d newsletter-track` point to `s2 homeostat run` / `s2 newsletter run`. `s5 check-policy-sync --update` requires a second confirmation.

**Tests:**
- **Coverage test:** for every `CommandSpec` from the real tree, the explorer can select it and render its form without error (Pilot, looped).
- Filtering by text (`cover`, `isbn`) finds the right commands.
- Running `s2 status <slug>` from the explorer on a harness book shows the same output as the CLI.
- The warned commands show the warning and the pointer.

**Done when:** tests pass. At this point every command in every system is usable from the UI.

### Phase 2 — Book production (S1B, S1D, S2)

#### T2.1 — Books list and new book

**Scope:** Home lists every book under `books_dir` with tasks done/total, failed and stale counts, ISBN set or not, and last activity, newest first. "New book": slug (lowercase letters, digits, hyphens; must not exist), runs `init`, file picker for the source `.txt`, copies it into `s1b/source/` (refuses if a file is already there), opens the book screen.

**Tests:** fixture books in four states (fresh, partial, with a failure, with stale tasks) show correct counts and order; invalid and duplicate slugs are rejected without running `init`; the happy path creates the book through the CLI and copies the source; `manifest.yaml` is touched only by `init`.

**Done when:** tests pass.

#### T2.2 — Book screen: status

**Scope:** One book's tasks grouped by system from `orchestrator.book_status`: done (output path, "edited since run" marker), stale (invalidated by, ready to rerun or blocked), ready, blocked (reason, including source-folder problems), failed (error, ready to retry or blocked). Refreshes after every run and on a manual refresh key.

**Tests:** fixture ledgers covering every state and the `edited_since_run` flag render the right label and detail; zero and two source files show the blocking reason; the rows match `s2 status` for the same book (same tasks, same states).

**Done when:** tests pass.

#### T2.3 — Run a single task

**Scope:** On a ready, failed, stale or done row: "Run" resolves the input exactly as the orchestrator does (the dependency's ledger `output`, or the single source file), shows the task's engine options as a form (`--document`, `--template`), confirms per Decision 5, runs `pipeline.py <system> <task> <input> [options]`. Re-running a done task warns that its output will be overwritten and that downstream tasks will become stale. For `s1b translate` with eTranslation, the confirmation states the expected wait and the resume behaviour.

**Tests:** cleanup then translate from the UI with fakes records both done; the confirmation appears for `llm_text`/`translation` tasks and not for `metadata`; declining runs nothing; blocked rows have Run disabled; re-running cleanup shows the stale warning and the screen then shows downstream rows stale; `--template` reaches the argv when set.

**Done when:** tests pass.

#### T2.4 — Run the pipeline

**Scope:** "Run pipeline" with: all, only S1B, only S1D, one step. Confirmation lists currently ready provider-calling tasks and notes that more become ready as the run proceeds. Streams `s2 run`; shows the done/failed summary; refreshes.

**Tests:** "only S1B" on a fresh harness book completes S1B (minus `format` without LibreOffice); "one step" runs one task; one injected failure appears in the summary while siblings complete; the command hint shows the exact `s2 run …` line.

**Done when:** tests pass.

#### T2.5 — Outputs and book facts

**Scope:** For done/stale rows: open file, open folder. For the book: open book folder, open `manifest.yaml` (read-only intent; opened in the operator's editor), set ISBN (`book set-isbn`, with a basic 10/13-digit check and confirmation if one is already set).

**Tests:** each open action calls `open_path` with the right path (patched); actions are disabled for rows without output; set-ISBN builds the right argv and the book list shows the new ISBN after refresh.

**Done when:** tests pass.

### Phase 3 — Print prep (S1C)

#### T3.1 — Print prep screen

**Scope:** Three forms over the existing `s1c` commands, with file pickers and millimetre defaults: check (optional expected trim), set interior, set cover (front+back width without spine, height, spine, bleed). The cover form shows the expected cover PDF page size as you type, `(trim_w + spine_w + 2·bleed) × (trim_h + 2·bleed)`, so a wrong export size is caught before running. Output path defaults to `<input>_print.pdf`. After running, offers "check the result" pre-filled with the output. Workflow note from the cheat sheet (export the PDF from LibreOffice by hand first; nothing here touches the ledger) shown on the screen.

**Tests:** the size preview matches the cheat sheet example (322 × 235 mm); argv for each form matches the cheat sheet examples; running set-cover on a generated PDF and then check shows the explicit TrimBox and BleedBox; the default output name never overwrites the input.

**Done when:** tests pass.

### Phase 4 — Performance (S3)

#### T4.1 — Dashboard

**Scope:** Portfolio table and per-book table from `lib/dashboard` and `lib/formatting`: tasks done, API cost, compute time, revenue by currency, reported margin, in-pipeline span; per task duration, provider/model, usage, cost, stale marker. Deviation flags (ADR 010) shown as a marker plus a summary line, exactly as the CLI words them. Label "Margin (API)" kept, with the CLI's explanation of what it excludes.

**Tests:** for the T0.4 fixture portfolio, every displayed figure equals the corresponding `s3 dashboard` output; `cost_usd: null` shows "-" with the omission note; a flagged book and a flagged task show the marker and line.

**Done when:** tests pass.

#### T4.2 — Sales ingest

**Scope:** Pick a book (books without an ISBN are shown with a "set ISBN first" action), pick a CSV, format auto-detect by default or forced from the list of known formats, run `s3 sales-ingest`, show the receipt (rows added/skipped), refresh the dashboard.

**Tests:** both CSV fixtures ingest through the UI; a second ingest of the same file reports 0 added; a book without ISBN cannot start an ingest; an unrecognised CSV shows the CLI's error and the `--format` choice.

**Done when:** tests pass.

### Phase 5 — Strategic intelligence (S4) and outbound articles (S1D)

#### T5.1 — Intelligence screen

**Scope:**
- Scan (no LLM; network): runs `s4 scan`; warns once when no scan exists yet that the first one surfaces many old items (ADR 003 point 9).
- Briefing: runs `s4 briefing` on the newest `intelligence/s4/scan/<date>/combined.txt`.
- Scan and brief: both in sequence; the briefing does not start if the scan fails.
- Content strategy: runs `s4 content-strategy` on a chosen briefing (newest by default).
- Article draft (S1D, ADR 012): runs `s1d article-draft` on a chosen content-strategy output.
- Lists of past scans, briefings, content strategies and article drafts, newest first, each openable; the date of the last scan.

**Tests:** newest-date resolution with several dated fixture folders; "scan and brief" stops on a failed scan (runner stubbed); with a stubbed `httpx.get`, scan then briefing then content-strategy then article-draft through the UI with fakes produce the four outputs; lists order and open actions.

**Done when:** tests pass.

### Phase 6 — Periodic pipelines (S2 homeostat, S2 newsletter, S5 decision log)

#### T6.1 — Homeostat

**Scope:** Last outcome of each stage (`orchestrator.periodic_status`), run the chain (`s2 homeostat run`, with "one step"), open the latest `homeostat.html` in the browser, decision log list (`homeostat.load_decisions`), and "log a decision" (tension + decision fields → `s5 log-decision`). The screen states that the homeostat reads the latest S4 briefing and the S3 portfolio, and shows the date of the briefing it will use.

**Tests:** status rows for never run / done / failed fixtures; running the chain with fakes produces the HTML and the open action targets it; a failed first stage leaves later stages not run and the screen shows it; logging a decision appears in the list after refresh.

**Done when:** tests pass.

#### T6.2 — Newsletter

**Scope:** Stage status; "this month's notes": creates `newsletter/notes/YYYY-MM.txt` if missing (empty) and opens it in the editor, with a warning on the run button when it is missing or empty; run the chain (`s2 newsletter run`); repeat warnings from `newsletter-track` highlighted in the run summary; lists of already-featured explorers and dishes; open the final newsletter.

**Tests:** notes file created at the right path for a frozen date; missing notes produce the warning; a second run with the same fake explorer shows the repeat warning; lists match `featured_explorers.yaml` / `featured_dishes.yaml`.

**Done when:** tests pass.

### Phase 7 — Identity and policy (S5)

#### T7.1 — Candidates

**Scope:**
- List every brief in `candidates/s1a/briefs/` with state: not evaluated; evaluated with the `<verdict>` value; decision recorded (from `calibration.yaml`) with the decision text.
- New candidate: slug → `candidate new` → create the empty brief file → open it in the editor. The screen shows the brief guidance from ADR 006 point 5 as a reminder (not written into the file).
- Evaluate (`s5 evaluate`), open verdict, record decision (`candidate record-decision` with acquired / declined / deferred or free text).
- Calibration view: verdict vs decision counts (how often "Encaja" ended in "declined" and so on), read from `calibration.yaml`.

**Tests:** list states from fixtures (`verdict_sample.txt`, a calibration file); a verdict file with no `<verdict>` tag shows "?" rather than failing; new candidate → evaluate → record decision through the UI with fakes ends with a calibration entry; duplicate slugs rejected; calibration counts match the fixture.

**Done when:** tests pass.

#### T7.2 — Policy sync

**Scope:** Show whether `prompts/s5/policy_evaluation_task.txt` is in sync with `docs/vsm.md`'s Editorial Policy section (`policy_check`), with stored and current hashes. When out of sync: buttons to open both files, and "mark as synced" (runs `s5 check-policy-sync --update`) behind a confirmation that the prompt was reviewed. Also shown as a warning on the candidates screen and in Health.

**Tests:** in-sync and out-of-sync states using temporary copies of the two files (the functions' path parameters), never the real ones; "mark as synced" requires the confirmation and builds the right argv.

**Done when:** tests pass.

### Phase 8 — Cross-cutting

#### T8.1 — Health screen

**Scope:** Read-only report of what would make runs fail:
- Config found and parsed; LLM provider and model set; the environment variables that provider needs are present (shown as set/missing, never the values); same for the translation provider; eTranslation `receiver_url` not still the example placeholder.
- Pricing configured or not, per provider.
- For every task in every `tasks.yaml`: its `prompt`, `reduce_prompt`, `metadata_config`, `style_config` and `sources_config` files exist.
- LibreOffice found; the template named in `templates/format_styles.yaml` exists.
- Policy sync state (T7.2).
- Data folders exist.
- Each item links to the screen or file that fixes it.

**Tests:** fixture configs and temporary manifests with each item broken produce the matching warning; an API key value set in the environment never appears in the rendered screen; the real repository with the operator's files reports no errors (run locally, skipped where personal files are missing).

**Done when:** tests pass.

#### T8.2 — Live smoke script

**Scope:** `tests/live/smoke.py`, run explicitly: `uv run python tests/live/smoke.py --scope core|full [--skip-translate] [--yes]`. It uses the operator's real config and `.env`, but a temporary workspace for all data folders, and builds every command with `tui/services/commands.py` so the UI's argv building is exercised against real providers.

- `core` (4 LLM calls): `s1b cleanup` on `tiny_source.txt`, `s1d brief` on the cleaned text, `s1d synopsis`, `s5 evaluate` on `tiny_candidate_brief.txt`.
- `full` (about 17 LLM calls and 1 translation): `s2 run` on a book made from `tiny_source.txt` (`format` only if LibreOffice is available), `s4 briefing` on `tiny_combined_scan.txt`, `s4 content-strategy`, `s1d article-draft`, `s5 evaluate`, `s2 homeostat run`, `s2 newsletter run` (with a one-line notes file).
- Before running: prints the provider, model, planned call count, the translation provider and its expected wait (eTranslation can take minutes even for a paragraph), and the cost ceiling; asks for confirmation unless `--yes`. Warns when the planned call count exceeds 25% of a known daily quota (for example the Google AI Studio free tier).
- Refuses to start if any input file exceeds 1,500 characters.
- After each command, sums `cost_usd` across the workspace ledgers; stops if the total exceeds `PIPELINE_LIVE_MAX_USD` (default 0.10).
- Prints a pass/fail line per command, total calls, total cost, and the workspace path (kept for inspection, deleted with `--clean`).

**Tests:** a unit test with the runner stubbed checks the planned call counts for both scopes, the size refusal and the ceiling stop; the script never runs as part of `unittest discover` (it is not named `test_*`).

**Done when:** one `core` and one `full` run pass on the operator's machine, and their call count, duration and cost are recorded in this ADR's implementation notes.

#### T8.3 — Documentation

**Scope:** README "Using the UI" section: install, launch, what each screen does, the one-run-at-a-time rule, how to run the tests and the smoke script. Note at the top of `docs/cheat_sheet.txt` that the UI's command explorer is generated from the CLI and is always current, and that the cheat sheet's workflow notes (S1C sizing) are reproduced on the print prep screen. Removal procedure from Decision 1. This ADR's status set to Implemented, with implementation notes in the style of ADRs 003–022.

**Tests:** every command in the new README section run once on the operator's machine.

**Done when:** done.

---

## Alternatives considered

- **Local web app (FastAPI + browser).** Better for visual dashboards and previews, but two moving parts, a server to start, and pressure toward hosting and multi-user use. Deliverables are opened externally anyway, and the homeostat already produces an HTML dashboard. `textual serve` keeps a browser option open.
- **Packaged desktop app (Tauri/Electron).** Heaviest to build and maintain, per-OS builds and signing, and it does not remove the Python/`uv`/LibreOffice requirements.
- **Separate repository.** Same-repo with an isolated package, an optional extra, and a tested "nothing imports `tui`" rule gives the same clean removal, while letting the UI reuse `lib/` and fail tests immediately when `lib/` changes.
- **Workflow screens only, no generic explorer.** Every new command would need UI work, and the UI would drift exactly like the cheat sheet. Rejected.
- **UI runs engines in-process.** Blocks the UI during long runs, risks a second ledger-writing path, makes cancel and crash isolation harder. Rejected.
- **pytest.** Not needed; the project's existing tests are `unittest`, and Textual's test API works under `unittest.IsolatedAsyncioTestCase`.
- **Real providers in automated tests, with tiny inputs.** Still uses daily quotas and the network on every run, and eTranslation alone can take minutes. Kept for the explicit smoke script only.

---

## Consequences

**Easier:**
- Running anything no longer depends on remembering commands, arguments or paths; the explorer always matches the CLI.
- Every action shows its CLI equivalent, so the terminal stays a usable fallback.
- Fake providers and the harness give the whole pipeline a free, offline regression suite across every system, useful with or without the UI.
- Health surfaces configuration drift (missing prompts, template mismatches, placeholder receiver URL, out-of-sync policy prompt) before a run fails.

**Harder / needs care:**
- `lib/` read functions are now used by the UI too; changing their signatures means updating `tui/services/state.py` (tests will catch it).
- The ledger is not locked: running the CLI in another terminal while the UI runs a task can lose a ledger write.
- Fake output satisfies structural contracts (`<overview>`, `<verdict>`, `<explorer>`, `<dish>`), not quality. Prompt reliability still needs the smoke script or real runs.
- Workflow screens encode folder conventions from earlier ADRs (dated run folders, `notes/YYYY-MM.txt`, `s1a/briefs/`). If those conventions change, those screens change too; the explorer does not.

---

## Open questions for the operator

1. Is Windows the only OS the UI must be tested on?
2. Smoke script: is a $0.10 ceiling right, and should `full` include translation by default given eTranslation's wait?
3. Candidate briefs and newsletter notes open in an external editor (proposed). Would you rather type them in a text box inside the UI?

**Answers (2026-10-22):**

1. Windows only. Tests and manual checks target Windows; the code stays portable (`click.launch`, SIGINT branch in the runner) but macOS and Linux are not exercised.
2. The ceiling is **$0.10** (`PIPELINE_LIVE_MAX_USD` default, as written). The `full` scope **skips translation by default**: T8.2's `--skip-translate` becomes the default behaviour of `full`, and a `--with-translate` flag opts in (it keeps the "expected wait" preview).
3. Candidate briefs and newsletter notes open in the operator's **external editor** (`click.launch`). No in-UI text box.

---

## Verification notes

*Filled in by T0.1 on 2026-10-22. Environment: Windows 10, `uv` 0.11.25, `uv run` interpreter Python 3.12.13 (the system `python` is 3.14, outside `requires-python`), Click 8.4.2, baseline `unittest discover -s tests`: 90 tests passing.*

### "Facts checked in the code"

All confirmed by reading the code, with these corrections and additions:

- **Command count.** The Click tree has **41 leaf commands** (10 top-level entries): `book set-isbn`, `candidate new|record-decision`, `init`, `s1b` ×5, `s1c` ×3, `s1d` ×12 (including the three newsletter stages and `article-draft`), `s2 run|status` plus `s2 homeostat run|status` and `s2 newsletter run|status`, `s3 dashboard|sales-ingest`, `s4` ×3, `s5` ×6 (`evaluate`, `log-decision`, `check-policy-sync`, and the homeostat trio).
- **`s3 sales-ingest` is registered twice** (manifest-driven, then overridden by the hand-written command with `book_slug csv_file`). The tree therefore holds only the hand-written one; the explorer needs no special case.
- **The operator's local `config.yaml` selects Mistral** (`mistral-medium-latest`), not the example's Z.ai; eTranslation is configured with a real `receiver_url`. The "configured defaults" fact describes `config.example.yaml`. Nothing in the UI assumes either provider.
- **`s1b format` does not use LibreOffice.** `engines/odt_format.py` writes the `.odt` with `odfpy` (pure Python). It needs only `templates/format_styles.yaml` and the `.odt` template it names, both personal and gitignored. LibreOffice is needed only for the manual PDF export before S1C. Consequences: the "LibreOffice tier" in T0.4 is really a "personal template present" tier, and Health (T8.1) reports LibreOffice as informational, not as something that would make a run fail. `s2 run --only s1b` on the operator's machine therefore completes `format` too.
- **Fake provider and `llm.model`.** `get_llm_provider` rejects a config with no `llm.model` before dispatching on the provider. The `fake` provider keeps that check (the test config sets `model: fake-1`). `_resolve_default_max_chars` already falls back to 8000 for any provider that is neither `z-ai` nor `mistral` and declares no `limits.max_output_tokens`, so no change is needed for `fake`.

### 1. Side effects of `import pipeline`

Beyond `load_dotenv()` and command registration there are three:

1. `import pdf_bleed_tool` (pikepdf import only, no work at import time).
2. `build_system_group()` imports every engine module named in the manifests (`deepl`, `feedparser`, `httpx` load transitively) and reads `systems/<name>/tasks.yaml` **relative to the current directory**.
3. **Silent failure outside the repository root.** `load_system_tasks()` returns `[]` when the manifest file is missing, so importing `pipeline` from another directory produces a Click tree where `s1b`, `s1d` and `s4` have **zero commands** and `s3`/`s5` lose their generated ones, with no error (verified from a scratch directory). `services/context.py` must therefore `chdir` to the repository root before the tree is built or any `lib/` function is called, and tests must run with the repository root as working directory (already a rule above). `pipeline.py ui` runs after import, so it receives a correct tree when launched from the repository root; the guard covers other launch directories for everything done afterwards.

Nothing else touches disk or the network at import time.

### 2. Direct invocation of `s5 homeostat-scan` and `s1d newsletter-scan`

**Confirmed by reading `_build_command`.** Both engines declare `CLI_ARG = "none"`, so the generated callback uses `paths.intelligence_root(config)` as root. Run directly, `s5 homeostat-scan` writes `intelligence/s5/homeostat-scan/<date>/combined.txt` and updates `intelligence/manifest.yaml`, and reads the decision log from `intelligence/decisions.yaml` (which does not exist), instead of using `homeostat/`. `s1d newsletter-scan` likewise looks for `notes/YYYY-MM.txt` and the featured lists under `intelligence/` instead of `newsletter/`. The chains (`s2 homeostat run`, `s2 newsletter run`) pass the correct root. T0.4 pins this behaviour with a test so a future fix is noticed. Not fixed here.

The other four stage commands (`s5 homeostat`, `s5 homeostat-render`, `s1d newsletter`, `s1d newsletter-track`) take a file and resolve the root by walking up to `manifest.yaml`, so they work when handed a file inside `homeostat/` or `newsletter/`. The explorer still warns on all six, as written in T1.5: they are stages of a chain, and running one alone skips the others.

### 3. Windows: `CTRL_BREAK_EVENT` against a child in a new process group

Tested with an `asyncio.create_subprocess_exec(..., creationflags=CREATE_NEW_PROCESS_GROUP)` parent and `send_signal(CTRL_BREAK_EVENT)`:

| Child | Result |
|---|---|
| plain `time.sleep(30)` inside `try/except KeyboardInterrupt` | Child dies at once, exit code `0xC000013A`. **No `KeyboardInterrupt`**, so `_run_recorded()` would record nothing. |
| `signal.signal(signal.SIGBREAK, signal.default_int_handler)` (the one-liner this ADR first proposed) | Still running after 8 seconds. Python only wakes a blocking `time.sleep()` for SIGINT, not SIGBREAK, so the interrupt waits until the sleep ends. For eTranslation (30-second polls) that is longer than the 10-second grace. Rejected. |
| `SetConsoleCtrlHandler` callback (via `ctypes`) calling `_thread.interrupt_main()` | Works only when the main thread executes bytecode (a loop of short sleeps: `KeyboardInterrupt` after 0.3 s). A single long `time.sleep()` is not woken. |
| Same callback, plus `SetEvent` on the interpreter's own SIGINT event (`ctypes.pythonapi._PyOS_SigintEvent()`) | **`KeyboardInterrupt` immediately, even inside `time.sleep(30)`.** This is what a real Ctrl+C does. |

**Decision.** `lib/interrupt.py` (pipeline side, not in `tui/`) provides `install_graceful_break()`: on Windows it registers that console handler (using the private `_PyOS_SigintEvent` only inside a `try`, falling back to `interrupt_main()` alone); on other platforms it does nothing, because SIGINT already raises `KeyboardInterrupt`. `pipeline.py`'s `__main__` block calls it only when `VIABLE_IMPRINT_GRACEFUL_BREAK` is set, so normal terminal use is unchanged. Click turns the `KeyboardInterrupt` into `Aborted!` with exit code 1 after `_run_recorded()` has written the `failed` entry. Like Ctrl+C in a terminal, a blocking socket read is not interrupted until it returns or times out; the 10-second grace then ends in a terminate and an unchanged ledger, which the UI reports. T1.3's integration test asserts the `failed` ledger entry on Windows.

### 4. CSV headers the sales providers match

- **IngramSpark** (`providers/sales/ingramspark.py`): detected when the header contains `ISBN13` and `Total Comp`. Parsed columns: `ISBN13`, `Net Quantity`, `Total Comp`, `Currency`, `Comp Period Start Date`, `Comp Period End Date`. One row per ISBN and period.
- **KDP** (`providers/sales/kdp.py`): detected when the header contains `ASIN/ISBN`, `Royalty` and `Marketplace`. Parsed columns: `ASIN/ISBN`, `Net Units Sold` (or `Units Sold`), `Royalty`, `Currency`, `Royalty Date` (or `Transaction Date`). Transaction-level rows, aggregated per ISBN and currency.

The fixtures use exactly these headers. The file docstring still warns they are best-effort, not verified against live exports.

### 5. `uv sync --extra tui`

Works with the current layout. Verified in a scratch copy: adding `[project.optional-dependencies] tui = ["textual>=0.80"]` to `pyproject.toml` and running `uv lock` adds Textual 8.2.8 plus `rich`, `markdown-it-py`, `mdurl`, `pygments`, `platformdirs`; `uv sync --extra tui` installs them. `uv run` keeps already-installed extras (inexact sync), while a plain `uv sync` removes them, so the README tells the operator to run `uv sync --extra tui` once and to re-run it after any plain `uv sync`.

### 6. Test layout: `tests/tui/` collides with `tui/`

`unittest discover -s tests` makes `tests/` the top-level directory, so a package `tests/tui/` with an `__init__.py` would be imported as the top-level module `tui`, shadowing the real `tui/` package. Without an `__init__.py` discovery skips the folder silently. **Resolution:** `tests/tui/` has no `__init__.py`, its modules are named `test_tui_*.py` (unique across `tests/`), and `tests/test_tui_suite.py` loads them through the `load_tests` protocol with `tests/tui/` as its own top level. The documented command `uv run python -m unittest discover -s tests` is unchanged. Test modules that need Textual skip themselves when it is not installed; service tests (no Textual import) always run.

### Tasks corrected as a result

- **T0.3 / T0.4:** the "LibreOffice" tier is a personal-template tier (see "`s1b format` does not use LibreOffice" above); a harness helper patches the manifest loader so periodic chains and `run_book` can run in-process against example prompts on a clean clone.
- **T1.1:** adds `lib/interrupt.py` and the `pipeline.py` `__main__` hook (Decision 6), plus the test-layout shim.
- **T1.3:** the Windows cancel test asserts a `failed` ledger entry (mechanism confirmed).
- **T8.1:** LibreOffice is informational.

---

## Implementation notes

*Appended per task as each one is completed.*

### Phase 0 — Groundwork

**T0.2 — Fake providers.** `providers/llm/fake.py` and `providers/translation/fake.py`, wired into `get_llm_provider` / `get_translation_provider` with a shared option reader (`fail`, `delay_seconds`, validated). The fake LLM embeds an excerpt of its input (first 200 characters) with `<` and `>` replaced by `‹` `›`, so a fake output fed to the next stage (briefs feed the expansion tasks) can never open a second `<overview>` that a downstream regex would match instead of the real one. Consequence: the original manuscript text survives only the first fake stage; later stages carry nested fake boilerplate. Tests assert structure, not prose. `llm.model` is still required for `fake` (see Verification notes). README gains a "Tests" section saying the fakes are for tests only.

**T0.3 — Harness.** `tests/harness.py` as specified, plus three additions the ADR did not list:
- `tests/example_pipeline.py`, an "example-mode" launcher: `pipeline.py` with `task_loader.load_system_tasks` replaced by `harness.example_tasks` (prompts to `prompts/examples/`, metadata and sources configs to the fixtures) before `pipeline` is imported, and `httpx.get` stubbed to serve `tiny_feed.xml`. `run_cli(..., example=True)` uses it. Reason: a subprocess cannot be patched from the test process, and without it every CLI case would skip on a clean clone and the UI's subprocess-based tests (T5.1 in particular) could not stub the network. The launcher is only ever used by tests; the UI shows the operator `pipeline.py` commands as usual.
- `GuardedCase`, a `TestCase` that asserts the operator's real data folders are unchanged after each test, and a suite-level guard: `harness.SUITE_GUARD` takes its listing when the harness is first imported and `tests/test_zz_real_data_guard.py` (sorts last) compares it after everything has run.
- `seed_portfolio(ws)`: four hand-written ledgers (exact numbers) with a cost outlier, a duration outlier, USD and EUR revenue and a task with no cost, reused by the S3 baseline, T0.5 and T4.1.
`require_personal_files()` also checks the `.odt` template named inside `format_styles.yaml`. `example_manifests()` patches the manifest loader in-process (for `orchestrator.run_book` and `run_periodic`).

**T0.4 — Baseline.** `tests/test_pipeline_baseline.py` (25 tests, about 60 seconds, mostly subprocess start-up). Differences from the ADR's wording:
- The rendered homeostat page is `homeostat/s5/homeostat-render/<date>/combined.html`, not `homeostat.html` (the render task keeps its input's file stem). Later tasks find it by glob.
- `s1b format` does not use LibreOffice, so `s2 run --only s1b` completes `format` whenever the personal template exists; the test expects `failed` for `format` only when it does not.
- "Independent siblings still run" is tested on S1D: after S1B and `brief` succeed, `fail: true` is switched on and `s2 run --only s1d` completes `metadata` (no LLM) while the six LLM expansions fail (exit 1, `Done: 1, Failed: 6`); switching it off and re-running retries them.
- The briefing case copies `tiny_combined_scan.txt` into `intelligence/s4/scan/<date>/`, because a task input must live under a folder with a `manifest.yaml` for the root to resolve.
- The direct-invocation root problem (Verification notes, item 2) is pinned by two tests that will fail when it is fixed.
- S4 `scan` runs in-process with `engines.feed_scan.httpx.get` patched; S1C PDFs are generated with `pikepdf`; `check-policy-sync` is run without `--update`, and the test asserts the committed prompt and `docs/vsm.md` are byte-identical afterwards.


**T0.5 — Formatters.** `lib/formatting.py` holds `format_duration`, `format_cost`, `format_margin`, `format_revenue`, `format_span` (public names, same bodies); `pipeline.py` calls them as `formatting.format_*` and no longer imports `datetime.date`. Golden files `tests/fixtures/dashboard_{portfolio,book-a,book-b,book-d}.golden.txt` were captured from the unmodified code on the `seed_portfolio()` fixture before the move and the post-move output matches them exactly (compared with line endings normalised to LF, since Windows turns `\n` into `\r\n` on the subprocess pipe). `book-d` covers the cost flag, the duration flag, USD revenue and a margin; `book-b` covers a task with no cost (`-`); `book-d` also covers a sub-cent cost (`$0.000100`).

### Phase 1 — UI foundation

**T1.1 — Package, extra, entry point.** `tui/` per the Skeleton (`app.py`, `app.tcss`, `services/context.py`, `screens/home.py`, empty `widgets/`; the other modules arrive with the task that needs them). `pyproject.toml` gains `[project.optional-dependencies] tui = ["textual>=0.80"]`; `uv.lock` was regenerated (Textual 8.2.8). `pipeline.py` gains `ui` (checks `import textual`, prints `Install the UI with: uv sync --extra tui` and exits 1 otherwise, then lazily imports `tui.app.run_app`) and a `main()` that calls `lib/interrupt.install_graceful_break()` when `VIABLE_IMPRINT_GRACEFUL_BREAK` is set (Decision 6). The isolation scan allows exactly one import of `tui` outside `tui/` and `tests/tui/`: `from tui.app import run_app` inside the `ui` function. `Context.load()` resolves `--config` against the launch directory, then changes into the repository root. `Context.pipeline_script` is the test seam: it defaults to `<repo>/pipeline.py` and tests point it at `tests/example_pipeline.py`. Test layout as decided in Verification notes, item 6 (`tests/test_tui_suite.py` shim, no `__init__.py` in `tests/tui/`). The ADR's test "textual import made to fail" runs `pipeline.py` in a subprocess with `sys.modules['textual'] = None`.

**T1.2 — Command model and argv builder.** `tui/services/command_model.py` (`build_model`, plus `group_helps` for labelling the explorer's tree) and `tui/services/commands.py` (`base_argv`, `argv_for`, `display`; the only module that spells `--config`, enforced by a scan test). The model has **42** leaf commands: the 41 pipeline commands plus `ui` itself. Findings and choices:
- Click 8.4 reports "no default" as `Sentinel.UNSET`, not `None`; the model maps it to `None`.
- `ParamSpec` gains `path_must_exist` (a `click.Path(exists=True)`, so the explorer offers a file picker) and `multiple` (`nargs != 1` or `multiple=True`; the argv builder repeats the option or extends the positionals). Flags use `type="text"`. No current command uses `multiple`.
- **The manifest-driven task commands carry no help text** (`s1b cleanup`, `s4 scan`, ... have an empty docstring because `_build_command` creates them without one). The explorer will show them with their arguments and no description. Left as is: adding help would change `lib/task_loader`, outside this ADR; a one-line `help:` key in each manifest would fix it.
- `argv_for` leaves out an option equal to its default (numerically, for floats), an unticked flag and empty values, and puts arguments before options as the cheat sheet does. A positional starting with `-` makes it emit the options first and a `--` separator. It raises `MissingParameter` or `InvalidValue` (both `ValueError`) with the parameter named.
- `display()` replaces the interpreter and script with `uv run python pipeline.py`, drops `--config` when it names the repository's own `config.yaml`, and quotes for the operator's shell: on Windows PowerShell-safe double quotes (single quotes when the value holds `$`, a backtick or `"`), POSIX quoting elsewhere. Non-ASCII characters are left unquoted.

**T1.3 — Runner.** `tui/services/runner.py`: `TaskRunner.run()` (asyncio subprocess, stdout and stderr merged, `on_line` per line, a 16 MiB line limit so a long echoed paragraph cannot raise), `cancel()` and `busy`, plus `RunnerBusy`. Choices beyond the interface in the ADR: `RunResult` also carries `seconds`; `returncode` is `None` and one explanatory line is emitted when the process cannot be started; `TaskRunner(cwd=...)` takes the working directory (the UI passes the repository root); if the task awaiting `run()` is cancelled from outside (the app closing), the child is killed so none is left behind. On Windows the child starts with `CREATE_NEW_PROCESS_GROUP`, gets `VIABLE_IMPRINT_GRACEFUL_BREAK=1` and is cancelled with `CTRL_BREAK_EVENT`; elsewhere `SIGINT`. After the grace period it is terminated (`forced=True`), then killed if it still does not exit within 5 seconds. Tested on the operator's OS (Windows): a script that cooperates stops with `cancelled=True, forced=False`; one that ignores the interrupt is terminated (`forced=True`); the integration test cancels `s1b cleanup` while the fake provider sleeps (`delay_seconds: 5`) and the ledger holds `status: failed, error: KeyboardInterrupt` — the Windows mechanism from T0.1 is confirmed end to end. The cancel took well under the 5-second sleep (interrupted, not waited out).

**T1.4 — App shell and navigation.** Built as three layers so the same pieces serve every later screen:
- *Services (no Textual):* `estimate.py` (Decision 5: provider and model, input characters, approximate calls using the engine's own chunk rules, cost from `pricing:` or "no pricing configured"; for chains it covers the provider-calling tasks that are ready now, or for a periodic chain its LLM stage with the input size marked unknown), `session.py` (`RunRequest`, `RunSession`: the output is kept so a view can detach and re-attach) and `files.py` (`open_path` via `click.launch`, `parse_saved` for `Saved:` lines). Estimates assume the output is as long as the input and four characters per token; they are approximations and say so. A task that cannot be split (`single_chunk`, no reduce prompt) whose input is too long reports 0 calls and "will stop before calling the provider".
- *Run modal (`screens/run.py`):* a view on the app's single `RunSession`. Confirmation only when the request has an estimate, warnings or `confirm=True`; zero-provider actions start at once. **Beyond the ADR:** the log can be hidden with `escape` while the run continues (a translation can take an hour) and reopened with `ctrl+r`; the header subtitle shows `Running: <title>`; the app shows a notification when a run ends and calls `run_finished(result)` on every screen on the stack so they can re-read state. The summary says what the ledger now holds (graceful cancel: recorded as failed and retryable; forced: ledger unchanged).
- *Shell:* `registry.py` lists the seven workflow screens with their keys (`e` explorer, `p` print prep, `d` dashboard and sales, `i` intelligence, `r` periodic, `c` candidates, `h` health); each is a placeholder naming the task that builds it until that task lands. Keys work on the home screen (so they never fight a text field), the command palette (`ctrl+p`) has "Open: ..." for each from anywhere, `escape` returns home, `?` shows keys and the one-run-at-a-time rule, `q` quits. `RunButton` is disabled while `app.busy` or while its screen gives a `blocked_reason`, which appears as its tooltip. `CommandHint` shows the equivalent command.
Textual's `DOMNode` has private members of its own (`_attach`, ...); a private helper of ours named the same silently replaces it and crashes mount, so screen and widget helpers use distinctive names (`_follow_session`, `_show_state`, ...).

**T1.5 — Command explorer.** `screens/explorer.py` (filter box, `Tree` grouped by top-level group with each group's help text as its label, detail panel, form, command hint, Run), `widgets/param_form.py` (text/number `Input`, `Select` for `click.Choice`, `Checkbox` for flags, a Browse button only for a `click.Path` that must exist), `screens/file_picker.py` (reusable modal: a `DirectoryTree` plus a folder box) and `services/command_notes.py` (the facts the Click tree does not carry: which commands are chain stages, and the extra confirmation for `check-policy-sync --update`). At this point all 42 commands are usable from the UI; the coverage test selects every one and checks its form has exactly one field per parameter.
- The six chain stages and `ui` itself show a warning and **no Run button**. Of the stage warnings, the two `*-scan` ones also say that run directly they use `intelligence/` (Verification notes, item 2).
- `s5 check-policy-sync --update` runs only after the run modal's confirmation; a test declines it and asserts the real prompt is byte-identical afterwards. The plain `check-policy-sync` runs without asking.
- Filtering matches the command name, its help and its parameter names, options and help (`cover` finds `s1c set-cover`; `isbn` finds `book set-isbn`).
- A missing required value is reported in the hint as "Cannot run yet: Missing required argument: INPUT_FILE" and nothing starts.
- Findings while building it: in Textual 8, `Select.BLANK` is just `False`; the "nothing chosen" sentinel is `Select.NULL` (`param_form.NO_SELECTION` papers over the rename). The file picker must focus its tree on open, or Enter re-submits the folder box instead of choosing the file (the Browse test caught it).
- Manifest-driven commands have no help text (T1.2 note), so their rows show only the name and the detail panel says "(this command has no description)".

### Phase 2 — Book production (S1B, S1D, S2)

**T2.1 — Books list and new book.** The home screen shows every book under `books_dir` (tasks done/total, failed, stale, ISBN or "not set", last activity), newest activity first; `Enter` on a row opens the book, `n` starts a new one. The counts come from `orchestrator.book_status` through `services/state.py`, so they equal `s2 status` by construction (a test compares them). "Last activity" is the latest `completed_at` / `attempted_at` in the ledger, or the manifest's modification time for a book that has never run. With no books the table is hidden and the menu has focus.
- *New book* (`screens/new_book.py`): name validated by `files.slug_problem` (lowercase letters, digits, single hyphens; must not exist), manuscript by `files.source_problem` (an existing `.txt`). Create dismisses the form and runs `init <slug>` through the run modal; when that run ends, `RunRequest.on_done` (new in this task) calls `finish_new_book`, which copies the manuscript into `s1b/source/` (`files.copy_source`, which refuses if a file is already there) and opens the book. A failed `init` copies nothing and leaves the log on screen. The resulting `manifest.yaml` is byte-for-byte what `init` writes (a test checks it).
- Accepting only `.txt` is a decision, following the ADR's wording ("source `.txt`"); the pipeline reads UTF-8 text.
- On success the run log is replaced by the book screen (the app pops modals when navigating); `init` output is two lines and the notification keeps its summary.
- `BookScreen` is a stub until T2.2.

**T2.2 — Book screen: status.** `screens/book.py` shows one book's tasks in a table grouped by system (the headings are the Click groups' own help text, so they cannot drift), with the same `[x] [~] [ ] [!]` marks and wording as `s2 status`: done (output, `(edited since run)`), stale (invalidated by, ready to rerun or blocked), ready, blocked (reason, including `no file in s1b/source/` and `2 files in s1b/source/ - expected exactly one`), failed (error, ready to retry or blocked). A detail panel follows the cursor. It refreshes when the screen is shown again, after every run (`run_finished`) and on `f5`. The formatting is in `services/state.py` (`describe_task`, `book_tasks`), which also decides `runnable` for T2.3: ready, failed-and-retryable, stale-and-rerunnable and done (re-running a done task is allowed, with a warning) are runnable; blocked ones are not. A test parses real `s2 status` output and compares task by task. Six ledger entries in the fixture cover every state, including a stale task that is itself blocked and a done task whose output was edited by hand.
Found by the full suite (not by the books tests run alone — it is timing-dependent): when a run ends, the app told every screen on the stack that it had finished and *then* ran the request's `on_done`; for a new book `on_done` pushes the book screen, so the notification reached a screen that was not mounted yet. The app now notifies only mounted screens, before `on_done` runs.

**T2.3 — Run a single task.** The book screen's right-hand panel shows the selected task, its engine options as a form (the task command's parameters minus the input file, so `s1b translate` gets `--document` and `s1b format` gets `--template`), the command it will run and a Run button (labelled Run task, Run again for a done task, Retry for a failed one). The button is disabled while another run is in progress and for blocked rows, whose reason is its tooltip. Running resolves the input, estimates (Decision 5) and opens the run modal.
- **Two small additive functions in `lib/orchestrator.py`**, the only pipeline-side change in this task: `resolve_input(root, key)` (the single source file or the dependency's ledger `output`, for *any* task including done ones; `ready_tasks()` skips done tasks, so it could not serve a re-run) and `downstream_of(key)` (the transitive dependents `cascade_invalidate()` walks). Both are read-only and tests pin them against `ready_tasks()` and `cascade_invalidate()`: for every ready task the input is the same; the stale set after a cascade equals `downstream_of()`. `ready_tasks`, `book_status` and `cascade_invalidate` themselves are untouched.
- Re-running a done task adds, before the estimate, "has already run. Running it again overwrites <output>" and, when other finished tasks depend on it, "These finished tasks will be marked stale: ...". The list is the done subset of `downstream_of()`, i.e. exactly the tasks the cascade will downgrade. A test runs a full S1B, re-runs cleanup from the UI and checks translate, ortho and copyedit become stale afterwards.
- For eTranslation the confirmation carries the estimate's note: expect a wait of minutes, up to two hours; running it again resumes the request already in flight (ADR 022).
- A task that fails shows as failed on the screen straight away (the screen refreshes when a run ends) with the ledger's error and "ready to retry".

**T2.4 — Run the pipeline.** Below the task panel the book screen has "Run the pipeline": a scope (all ready tasks, only S1B, only S1D) and a "one step only" checkbox, which together give the four cases in the ADR and their combinations (`--only` and `--step` are independent flags in the CLI). The hint shows the exact `s2 run <slug> [--only X] [--step]` line. The confirmation lists the provider-calling tasks that are ready now (from `estimate_book_run`, which uses `orchestrator.ready_tasks`) and says more become ready as the run proceeds; a pipeline with only zero-provider tasks ready (for instance only `metadata`) starts without asking. The run modal's summary now also states the outcome of the CLI's `Done: N, Failed: M` block (`services/output.chain_summary`), for example "1 task done, 6 failed: s1d.synopsis, ...", and the book screen refreshes when the run ends. Decision beyond the ADR: the button is disabled with the reason "Nothing is ready to run in this scope" when `ready_tasks` is empty, since `s2 run` would only print "Book complete" or nothing. `services/output.py` is also what T6.x will use for the periodic chains.

**T2.5 — Outputs and book facts.** The task panel gains Open file and Open folder (disabled unless the row has a recorded output that exists on disk, so a stale row whose file is gone, a failed row and a blocked row are all disabled), and a "This book" section with Open book folder, Open manifest.yaml (it opens in the operator's editor; a test checks the file's bytes are unchanged afterwards) and Set ISBN. All opens go through `files.open_path` (`click.launch`). Set ISBN runs `book set-isbn <slug> <isbn>` through the run modal; the typed value is normalised to plain digits (hyphens and spaces dropped) because System 3 joins royalty rows on the exact string (ADR 011) and platform CSVs carry plain digits. The check (`services/validate.normalize_isbn`) is the "basic" one the ADR asks for: 13 digits, or 10 characters with an optional final X; **no checksum**, so a mistyped but well-formed ISBN is accepted. When the book already has an ISBN the modal asks first, naming both values and noting that sales entries already ingested are kept. After the run the book screen's title and the home table show the new ISBN.
