# ADR 001 — Modular, Config-Driven Task Pipeline

**Status:** Implemented.

---

## Context

The pipeline currently hardcodes one Python module and one Click command per task: `cleanup`, `translate`, `ortho`, `copyedit`, `format` live in `systems/s1b/tasks/`, and `marketing` lives in `systems/s1d/tasks/`. Each is wired into `pipeline.py` as its own `@cli.command()`.

This works for us, but it does not serve the project's open-source goal. A publisher who wants a different System 1B pipeline — say, they don't need `ortho` because their texts are already typeset, or they want a fourth pass for fact-checking — has to fork the repository and edit Python. Three of our five 1B tasks (`cleanup`, `ortho`, `copyedit`) are also functionally identical: chunk text at paragraph boundaries, send each chunk to an LLM with a system prompt, reassemble, write to disk. They differ only in which prompt file they load. That duplication is itself a symptom of the hardcoding problem — the task identity is really just "a prompt," but our code treats it as "a bespoke Python file."

The goal, in the user's words: a system like 1B should be configurable by handing it a list of prompts for the tasks it needs to accomplish — convention over configuration, not fork-and-edit.

---

## Decision

1. **Each system owns a task manifest.** `systems/s1b/tasks.yaml` and `systems/s1d/tasks.yaml` each declare an ordered list of tasks for that system. This directly matches "1B has its own pipeline, 1D has its own pipeline" — a publisher can replace one system's manifest without touching another's.

2. **A small fixed set of generic engines replace bespoke per-task files.** An engine is the execution logic; a task is a named configuration of an engine. Four engines cover everything implemented so far:
   - `llm_text` — chunk input, call an LLM with a given system prompt, reassemble, write output. Replaces `cleanup.py`, `ortho.py`, `copyedit.py` (today: three near-duplicate files; tomorrow: one engine, three manifest entries).
   - `translation` — chunk input, call a translation provider, reassemble. Replaces `translate.py`.
   - `odt_format` — render markup to `.odt`. Replaces `format_odt.py`.
   - `llm_multi_doc` — single LLM call producing multiple split output documents. Replaces `marketing.py`.

   Adding a new LLM-driven text task going forward (e.g. a glossary pass, a fact-check pass) requires writing a prompt file and adding an entry to `tasks.yaml` — no Python.

3. **Convention over configuration for input/output folders.** By default, a task reads from the previous task's output folder (or `source/` if it's first in the manifest) and writes to a folder named after itself (`<task_name>/`, or `<task_name>/<lang>/` once a language-bearing engine has run). A manifest entry can override `input:` explicitly when a task needs to read from something other than its immediate predecessor — `marketing` in System 1D is the example: it doesn't chain from a prior 1D task, it reads from 1B's best available output.

4. **CLI is nested by system.** `pipeline.py s1b cleanup <file>`, `pipeline.py s1d marketing <file>`. Each system that exists gets an explicit Click group (matching the existing rule: only `s1b` and `s1d` exist today, because only those have real tasks). The tasks *within* a system's group are not hardcoded — they're registered dynamically at startup by reading that system's `tasks.yaml`. This means task names only need to be unique within their own system, not globally, and `pipeline.py s1b --help` becomes a live, accurate listing of whatever System 1B is currently configured to do.

---

## Detailed Design

### File layout

```
engines/
  __init__.py
  llm_text.py         # generic: chunk + LLM completion using a given prompt file
  translation.py      # generic: chunk + translation provider call
  odt_format.py       # generic: markup .txt → .odt
  llm_multi_doc.py     # generic: single LLM call → multiple split output files

systems/
  s1b/
    tasks.yaml
  s1d/
    tasks.yaml

prompts/
  cleanup_task.txt
  ortho_task.txt
  copyedit_task.txt
  marketing_task.txt
```

The `systems/s1b/tasks/*.py` and `systems/s1d/tasks/*.py` bespoke files are removed; their logic is absorbed into the four engines above.

### Manifest schema

`systems/s1b/tasks.yaml`:

```yaml
tasks:
  - name: cleanup
    engine: llm_text
    prompt: prompts/cleanup_task.txt
  - name: translate
    engine: translation
  - name: ortho
    engine: llm_text
    prompt: prompts/ortho_task.txt
  - name: copyedit
    engine: llm_text
    prompt: prompts/copyedit_task.txt
  - name: format
    engine: odt_format
```

`systems/s1d/tasks.yaml`:

```yaml
tasks:
  - name: marketing
    engine: llm_multi_doc
    prompt: prompts/marketing_task.txt
    input: copyedit   # override: reads 1B's output rather than a prior 1D task
```

### Engine interface

Every engine exposes one function with the same shape:

```python
def run(input_file: Path, output_dir: Path, config: dict, **params) -> Path:
    ...
```

`params` is whatever the manifest entry declares beyond `name`, `engine`, and `input` (e.g. `prompt` for `llm_text` and `llm_multi_doc`). The task runner resolves `input_file` and `output_dir` from the convention-over-configuration rule (or the manifest's explicit `input:` override) and calls the named engine.

**As implemented:** the signature ended up as `run(input_file, root, output_name, config, **params) -> Path` rather than receiving a pre-resolved `output_dir`. The pre-migration tasks had inconsistent output-folder logic (e.g. `format` only mirrors a language subfolder when the input path literally starts with `translated/`; `cleanup`'s output folder name doesn't match its task name). Preserving these exactly — required by "maintain existing functionality at all costs" — meant each engine needed to compute its own output path from `root` + `output_name`, rather than have a single shared convention applied uniformly upstream. The dynamic CLI layer (`lib/task_loader.py`) resolves `root` via `lib/paths.book_root` and stays otherwise generic.

### CLI mechanics

`pipeline.py` defines one Click group per existing system (`s1b`, `s1d`). At import time, each group loads its own `tasks.yaml` and registers one dynamically-built command per entry — the command's argument signature, help text, and behaviour all come from the shared task-runner plus the manifest entry's fields. No per-task function is hand-written.

### `init` simplification

`init` currently hardcodes a list of folders to pre-create (`cleaned/`, `translated/<lang>/`, `ortho/<lang>/`, etc.). Under this design `init` no longer needs to know what any system's pipeline looks like — it creates `source/` and `manifest.yaml` only. Every task already creates its own output directory on first run (`output_dir.mkdir(parents=True, exist_ok=True)`), so folders appear exactly when needed, named after whatever the manifest currently calls them.

---

## Alternatives Considered

- **Flat dynamic subcommands** (`pipeline.py cleanup <file>`, auto-generated from manifests but not nested by system) — rejected. Requires every task name to be globally unique across all systems, which fights the "each system owns its own pipeline" framing and would silently break if two systems independently chose the same task name.
- **Generic `run <system> <task> <file>` command** — rejected. Loses per-task `--help` text and is a bigger departure from the CLI shape already in daily use.
- **Keep one bespoke Python file per task, add only a manifest for ordering** — rejected. Doesn't achieve the actual goal (configure a system by handing it prompts); adding a task would still require writing Python.

---

## Consequences

**Easier:**
- Adding a new LLM-driven text task to any system: write a prompt file, add three lines to that system's `tasks.yaml`. No code.
- Forking a single system's editorial pipeline without touching the rest of the project.
- `pipeline.py <system> --help` is always an accurate reflection of that system's current configuration.

**Harder / needs care:**
- Engines must stay generic enough to cover real task variation. A task that needs logic genuinely different from the four existing engines (e.g. something stateful across chunks) still requires writing a new engine — appropriately rare, but worth naming as the boundary of "configuration" vs. "code."
- The `input:` override mechanism needs to be specified precisely so non-linear pipelines (like `marketing` reading 1B's output) remain a one-line manifest override, not a special case in code.

---

## Migration Checklist (completed)

- [x] Create `engines/` package: `llm_text.py`, `translation.py`, `odt_format.py`, `llm_multi_doc.py`
- [x] Port logic from `systems/s1b/tasks/*.py` and `systems/s1d/tasks/marketing.py` into the corresponding engines
- [x] Define the `tasks.yaml` schema loader — landed as `lib/task_loader.py`, kept distinct from the existing per-book `lib/manifest.py`
- [x] Rewrite `pipeline.py`: one Click group per existing system, tasks registered dynamically from each system's `tasks.yaml`
- [x] Delete `systems/s1b/tasks/*.py` and `systems/s1d/tasks/*.py` once their engines are live (including the now-unneeded `__init__.py` package scaffolding under `systems/`)
- [x] Simplify `init` to create only `source/` and `manifest.yaml`
- [x] Update README architecture section and all command examples to the nested CLI shape (`pipeline.py s1b cleanup <file>`)
- [x] Re-test the full pipeline end-to-end against the new CLI — verified against existing `books/test/` data; manifest keys and output paths matched the pre-migration behaviour exactly (including legacy inconsistencies, e.g. `formatted_formatted`), with `s1b format` re-run successfully through the new CLI

---

## Amendment (2026-07-02): system-scoped output folders

Point 3's "convention over configuration" rule placed every task's output folder directly under the book root, flat — `cleaned/`, `translated/es/`, `brief/es/`, `synopsis/es/`, etc. all side by side. This stopped being navigable once System 1D grew to eight tasks (ADR 002): a book folder listing mixed five System 1B stages with eight System 1D deliverables with no visual grouping, even though the CLI itself has always been grouped by system (`pipeline.py s1b <task>`, `pipeline.py s1d <task>`). Raised directly by the user after seeing a real book folder listing.

**Change:** every task's output now nests one level deeper, under the VSM system that produced it — `s1b/cleaned/`, `s1b/translated/es/`, `s1d/brief/es/`, `s1d/synopsis/es/`, etc. — mirroring the CLI's own grouping exactly. `manifest.yaml` is the sole exception, staying at the book root, since it's a cross-system record, not any one system's output.

**Mechanics:**
- `lib/paths.book_root()` no longer checks for a `source/` folder as a fallback signal — `source/` now lives under `s1b/source/`, so that check would incorrectly match `s1b/` itself as the book root. Detection now relies solely on `manifest.yaml`, which `init` always creates at the true root and which never moves.
- `lib/paths.stage_output_dir()` gained a `system` parameter (the *producing* task's system, e.g. `"s1d"` when running `s1d brief`) and prepends it to the output path. Note this is independent of whatever system the *input* file's path happens to start with — `s1d brief` reads from `s1b/copyedit/es/...` but writes to `s1d/brief/es/...`, matching who's writing, not who's being read.
- `lib/task_loader.py`'s `_build_command` now threads the system name (already known — it's the Click group being built) through to `engine.run()` as a new positional parameter, inserted right after `root`.
- Every engine's `run()` signature gained this `system` parameter: `llm_text.py`, `translation.py`, `metadata_doc.py` (all via the shared `stage_output_dir`), and `odt_format.py` (which computes its output path manually — its narrow "only mirror a language folder when the input path starts with `translated/`" check, noted above as a preserved legacy quirk, was kept exactly as-is, just shifted one index to account for the new leading system segment).
- `init` now creates `s1b/source/` instead of a bare `source/`.

**Migration:** any existing book folder created before this change has its content at the old flat paths. `books/test/` (this repo's own test fixture) was migrated by physically moving each folder under `s1b/` or `s1d/` and correcting the resulting stale `manifest.yaml` path entries to match. There is no automated migration command — a book folder is expected to be small enough in count that this is a one-time manual `mv`, same approach used here. Mixing an old flat book folder with the updated code will not crash, but will silently skip language-folder mirroring for any task reading from an old-style un-prefixed path (its relative path is one segment shorter than the new code expects) — migrate the whole folder before continuing to use it, don't mix old and new layouts within one book.
