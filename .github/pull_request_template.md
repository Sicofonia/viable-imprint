## What and why

<!-- What this changes and why. Link the ADR if there is one (docs/adr/NNN-...). -->

## Definition of Done

The full definition, with what each item means, is in [CONTRIBUTING.md](https://github.com/Sicofonia/viable-imprint/blob/main/CONTRIBUTING.md#definition-of-done). Tick what applies; write "n/a" next to what does not.

**Always**
- [ ] Tests added or updated, and the full suite passes (see CONTRIBUTING for running it in pieces)
- [ ] Real data folders untouched (the suite's guard test passes)
- [ ] README updated for anything a user can now do or must set up
- [ ] ADR implementation notes updated (or: no ADR, and why)

**If a command, option or argument was added or changed**
- [ ] `docs/cheat_sheet.txt` updated
- [ ] `--help` describes it
- [ ] Terminal UI: explorer form checked, and the dedicated screen (book, print prep, dashboard, intelligence, periodic, candidates) updated if it is affected

**If a screen or key in the UI changed**
- [ ] README "Using the UI" section updated

**If a setting, credential or personal file was added**
- [ ] `config.example.yaml` / `.env.example` updated, or a committed example added and the real file gitignored
- [ ] Health screen reports it if its absence would make a run fail

**If a task, engine or provider was added**
- [ ] Declared in `systems/<system>/tasks.yaml` with a prompt example; usage and cost recorded in the ledger

## Not verified or left out

<!-- E.g. a live run against a real provider, platforms other than Windows, known gaps. -->
