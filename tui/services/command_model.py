"""The Click tree as plain data: one `CommandSpec` per leaf command (ADR 023).

The command explorer and the argv builder are generated from this, so every
current and future command in every system is reachable with no UI change.
Plain Python; no Textual.
"""
import inspect
from dataclasses import dataclass, field

import click


@dataclass
class ParamSpec:
    name: str                  # Click parameter name (the key of a form's values dict)
    kind: str                  # "argument" | "option" | "flag"
    type: str                  # "path" | "choice" | "float" | "int" | "text" ("text" for flags too)
    required: bool
    default: object            # None when Click has no default
    choices: list = None
    help: str = None
    opts: list = field(default_factory=list)   # e.g. ["--trim-w"]; for an argument, [name]
    path_must_exist: bool = False   # click.Path(exists=True): the form offers a file picker
    multiple: bool = False          # nargs != 1 or multiple=True: the value is a list


@dataclass
class CommandSpec:
    path: list                 # e.g. ["s2", "homeostat", "run"]
    help: str
    params: list = field(default_factory=list)

    @property
    def name(self) -> str:
        return " ".join(self.path)

    def param(self, name: str) -> ParamSpec:
        return next(p for p in self.params if p.name == name)


def _default(param: click.Parameter):
    # Click 8.3+ reports "no default" as a Sentinel rather than None.
    value = param.default
    if value is None or type(value).__name__ == "Sentinel":
        return None
    return value() if callable(value) else value


def _type_name(param_type: click.ParamType) -> str:
    if isinstance(param_type, click.Path):
        return "path"
    if isinstance(param_type, click.Choice):
        return "choice"
    if isinstance(param_type, click.types.FloatParamType):
        return "float"
    if isinstance(param_type, click.types.IntParamType):
        return "int"
    return "text"


def _param_spec(param: click.Parameter) -> ParamSpec:
    if isinstance(param, click.Argument):
        kind = "argument"
    elif getattr(param, "is_flag", False):
        kind = "flag"
    else:
        kind = "option"
    type_name = _type_name(param.type) if kind != "flag" else "text"
    return ParamSpec(
        name=param.name,
        kind=kind,
        type=type_name,
        required=bool(param.required),
        default=_default(param),
        choices=list(param.type.choices) if type_name == "choice" else None,
        help=getattr(param, "help", None) or None,
        opts=list(param.opts),
        path_must_exist=isinstance(param.type, click.Path) and bool(param.type.exists),
        multiple=bool(param.multiple or param.nargs != 1),
    )


def _help(command: click.Command) -> str:
    return inspect.cleandoc(command.help or command.short_help or "")


def build_model(root: click.Group) -> list:
    """Every leaf command under `root`, depth first in name order, groups flattened
    into the command's `path`."""
    specs = []

    def walk(group: click.Group, prefix: list):
        for name in sorted(group.commands):
            command = group.commands[name]
            if isinstance(command, click.Group):
                walk(command, prefix + [name])
            else:
                specs.append(CommandSpec(path=prefix + [name], help=_help(command),
                                         params=[_param_spec(p) for p in command.params]))

    walk(root, [])
    return specs


def group_helps(root: click.Group) -> dict:
    """Group path (tuple) -> the group's help text, for labelling the explorer's tree."""
    found = {}

    def walk(group: click.Group, prefix: tuple):
        for name in sorted(group.commands):
            command = group.commands[name]
            if isinstance(command, click.Group):
                found[prefix + (name,)] = _help(command)
                walk(command, prefix + (name,))

    walk(root, ())
    return found
