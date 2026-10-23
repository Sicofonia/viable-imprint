# Contributing

Viable Imprint is a small editorial pipeline for a micro-imprint, designed system by system (see `docs/vsm.md`). Every real feature starts as an **ADR** in `docs/adr/` and is built from it task by task. A change small enough not to need an ADR (a bug fix, a wording change) skips that step, but not the Definition of Done below.

## Before you start

- Branch off `main` (or `origin/main` if yours is behind), one branch per feature or ADR.
- For anything new, read the ADRs that touch it. They record why things are the way they are, including dead ends.
- Windows is the platform the project has been tested on. Other platforms should work (the code uses portable calls) but are not exercised.

## Definition of Done

A change is done when everything that applies below is true. The pull request template links here; tick only what applies and say so for what does not.

### Always

- [ ] **Tests.** New behaviour has tests, and the full suite passes (see [Running the tests](#running-the-tests)). Tests run offline on the fake providers (`provider: fake`): no network, no API key, no quota. A bug fix has a test that fails without the fix.
- [ ] **Real data untouched.** Tests use temporary folders. The suite's guard test fails if your real `books/`, `intelligence/`, `candidates/`, `homeostat/` or `newsletter/` change.
- [ ] **README.** Anything a user can now do, must do differently, or must set up is in `README.md` (Setup, Running the pipeline, Using the UI or Tests, whichever fits).
- [ ] **ADR.** If the work has an ADR, its implementation notes record what was built, what differed from the plan and why, and what was left out. If it does not, a pull request description that says why is enough.

### If you add or change a command, an option or an argument

- [ ] **Cheat sheet.** `docs/cheat_sheet.txt` lists it, in the section of the system it belongs to.
- [ ] **`--help`.** `uv run python pipeline.py <command> --help` describes it. (Commands generated from `systems/*/tasks.yaml` have no help text today; see the known gaps in ADR 023.)
- [ ] **Terminal UI.** The command explorer (`e`) is generated from the CLI, so the command is already there; check that its form is sensible. Then decide whether a **dedicated screen** needs to change, and if it does, change it and its tests:
  - book tasks, pipeline run, outputs, ISBN: the book screen
  - `s1c`: print prep (`p`)
  - `s3`: dashboard and sales (`d`)
  - `s4` and `s1d article-draft`: intelligence (`i`)
  - `s2 homeostat`, `s2 newsletter`: periodic (`r`)
  - `s5` and `candidate`: candidates (`c`)
  
  A command that makes a provider call must also work with the UI's confirmation (`tui/services/estimate.py`).

### If you add or change a screen or key in the UI

- [ ] The "Using the UI" section of the README (keys, screen table) matches. `tests/tui/test_tui_docs.py` checks the keys and screen names.
- [ ] The UI still does only two things: reads state by calling `lib/` directly, and changes state only by running `pipeline.py`. Nothing outside `tui/` imports it (a test checks).

### If you add a setting, a credential or a file the pipeline needs

- [ ] **Config.** A new setting is in `config.example.yaml` with a comment saying what it does and what happens if it is unset. Secrets go in `.env` (and `.env.example`), never in config.
- [ ] **Personal files.** A new prompt, template or watchlist is personal: add a committed example (`prompts/examples/<system>/...`, or `<name>.example.yaml`), gitignore the real file, and add the setup step to the README.
- [ ] **Health.** If a missing value or file would make a run fail, the Health screen reports it (`tui/services/health.py`). A new provider or a new environment variable it reads must be added to the tables there; a test fails if they drift from `providers/__init__.py`.

### If you add a task, an engine or a provider

- [ ] **Manifest.** A task is declared in `systems/<system>/tasks.yaml` with its prompt example in `prompts/examples/`.
- [ ] **Ledger.** A task that calls a provider records usage and cost through the shared hook (`lib/task_loader.py`), so the dashboard and the UI's estimates see it.
- [ ] **Prompts that parse output** follow the reliability lessons in the ADRs (tags for what code parses, fixed headings for what a human reads).

## Running the tests

```bash
uv sync --extra tui                                                    # the UI tests need Textual
uv run python -m unittest discover -s tests                            # everything; about 12 minutes
uv run python -m unittest discover -s tests -p "test_[a-s]*.py"        # everything except the UI; about 2 minutes
uv run python -m unittest tests.tui.test_tui_explorer tests.tui.test_tui_shell   # chosen UI modules
```

The full suite is slow because most UI tests start a real `pipeline.py` subprocess. If you run it in a tool or a script with its own time limit, run it in pieces as above. Tests that start a subprocess or the UI are bounded: an asynchronous test fails after two minutes instead of hanging, and a watchdog ends a run stuck for five and prints where. Keep new tests of that kind bounded too (subclass `GuardedCase` from `tests/harness.py`, and give every subprocess a timeout).

Tests that need your personal prompts or templates skip, naming the missing file, on a clean clone.

### The live smoke test is never part of a pull request

`tests/live/smoke.py` calls real providers with your own keys and spends real quota. Run it yourself when you want to check a prompt or a provider change (`--dry-run` shows the plan without calling anything); do not ask a reviewer to, and do not add it to automation.

## Pull requests

- Keep one concern per pull request, and describe what changed outside the feature itself (shared code under `lib/`, `providers/`, `pipeline.py`).
- Say what you could not verify (for example, a live run).
- Commit messages say what changed and why in the first line.
