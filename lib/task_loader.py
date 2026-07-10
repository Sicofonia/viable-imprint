import importlib
import time
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml

from lib import manifest, metrics, paths

# "input" is reserved too: it's consumed by the System 2 orchestrator (ADR 004)
# to resolve a task's default input from the run-state ledger, not passed
# through to any engine.
_RESERVED_KEYS = ("name", "engine", "output", "input")


def load_system_tasks(system_name: str) -> list:
    manifest_path = Path("systems") / system_name / "tasks.yaml"
    if not manifest_path.exists():
        return []
    with open(manifest_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("tasks", [])


def build_system_group(system_name: str, help_text: str) -> click.Group:
    """Build a Click group for a VSM system, with one command per task
    declared in that system's tasks.yaml manifest.
    """
    group = click.Group(name=system_name, help=help_text)
    for task in load_system_tasks(system_name):
        group.add_command(_build_command(task, system_name))
    return group


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_recorded(run_fn, root: Path, system: str, name: str, config: dict):
    """Call an engine's `run_fn` (already bound to its args), time it, and
    record the outcome in the run-state ledger (ADR 004/005) — the one
    shared hook both manual single-task invocation and the System 2
    orchestrator go through, so `manifest.yaml`'s `tasks:` block stays
    accurate regardless of which path ran a task.

    `run_fn` may return either a bare `Path` (engines with no external
    provider call: `odt_format`, `metadata_doc`, `feed_scan`) or a
    `(Path, {"usage": {...}})` tuple (`llm_text`, `translation`) — see
    ADR 005. `lib.metrics.enrich()` turns raw usage into ledger fields
    (provider/model/cost_usd); tasks with no usage contribute none of those
    fields, only `duration_seconds`.
    """
    key = f"{system}.{name}"
    start = time.monotonic()
    try:
        result = run_fn()
    except Exception as e:
        manifest.record_task(root, key, status="failed", error=str(e), attempted_at=_now_iso(),
                              duration_seconds=round(time.monotonic() - start, 2))
        raise

    duration = round(time.monotonic() - start, 2)
    output_file, raw_metrics = result if isinstance(result, tuple) else (result, {})
    manifest.record_task(
        root, key, status="done",
        output=str(output_file.relative_to(root)), completed_at=_now_iso(),
        duration_seconds=duration, **metrics.enrich(raw_metrics, config),
    )
    return output_file


def run_task(root: Path, config: dict, system: str, task: dict,
             input_file: Path = None, **cli_kwargs):
    """Run one task's engine against a resolved `root`/`input_file`, recording
    the outcome in the run-state ledger. Shared by the CLI callback below
    (input_file resolved from what the user typed) and the System 2
    orchestrator (input_file resolved from the ledger — see
    `lib/orchestrator.py`), so both paths behave identically.
    """
    name = task["name"]
    engine = importlib.import_module(f"engines.{task['engine']}")
    output_name = task.get("output", name)
    extra_params = {k: v for k, v in task.items() if k not in _RESERVED_KEYS}
    arg_kind = getattr(engine, "CLI_ARG", "file")

    if arg_kind == "none":
        run_fn = lambda: engine.run(root, system, output_name, config, **extra_params, **cli_kwargs)
    else:
        run_fn = lambda: engine.run(input_file, root, system, output_name, config, **extra_params, **cli_kwargs)

    return _run_recorded(run_fn, root, system, name, config)


def _build_command(task: dict, system_name: str) -> click.Command:
    name = task["name"]
    engine = importlib.import_module(f"engines.{task['engine']}")
    cli_options = getattr(engine, "CLI_OPTIONS", [])
    arg_kind = getattr(engine, "CLI_ARG", "file")

    if arg_kind == "none":
        # For tasks with nothing to transform (e.g. System 4's `scan`, which
        # pulls from external sources per a config file rather than a given
        # input file). `root` comes from config instead of being resolved by
        # walking up from a file.
        def callback(_task=task, _system=system_name, **cli_kwargs):
            config = click.get_current_context().obj["config"]
            root = paths.intelligence_root(config)
            run_task(root, config, _system, _task, **cli_kwargs)

        return click.Command(name=name, params=cli_options, callback=callback)

    argument = click.Argument(["input_file"], type=click.Path(exists=True, dir_okay=False))

    def callback(input_file, _task=task, _system=system_name, **cli_kwargs):
        config = click.get_current_context().obj["config"]
        resolved = Path(input_file).resolve()
        root = paths.book_root(resolved)
        run_task(root, config, _system, _task, input_file=resolved, **cli_kwargs)

    return click.Command(name=name, params=[argument] + cli_options, callback=callback)
