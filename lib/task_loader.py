import importlib
from pathlib import Path

import click
import yaml

from lib import paths

_RESERVED_KEYS = ("name", "engine", "output")


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


def _build_command(task: dict, system_name: str) -> click.Command:
    name = task["name"]
    engine = importlib.import_module(f"engines.{task['engine']}")
    output_name = task.get("output", name)
    extra_params = {k: v for k, v in task.items() if k not in _RESERVED_KEYS}
    cli_options = getattr(engine, "CLI_OPTIONS", [])

    argument = click.Argument(["input_file"], type=click.Path(exists=True, dir_okay=False))

    def callback(input_file, _engine=engine, _system=system_name, _output_name=output_name,
                 _params=extra_params, **cli_kwargs):
        config = click.get_current_context().obj["config"]
        resolved = Path(input_file).resolve()
        root = paths.book_root(resolved)
        _engine.run(resolved, root, _system, _output_name, config, **_params, **cli_kwargs)

    return click.Command(name=name, params=[argument] + cli_options, callback=callback)
