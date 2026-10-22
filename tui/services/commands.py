"""The ONLY place that builds `pipeline.py` command lines (ADR 023).

Pure functions. Every action the UI runs, and every command it shows the
operator, goes through here, so what is displayed is exactly what is executed.
"""
import re
import sys
from pathlib import Path

from tui.services.command_model import CommandSpec
from tui.services.context import DEFAULT_CONFIG, Context

DISPLAY_PREFIX = "uv run python pipeline.py"

_SAFE_WORD = re.compile(r"^[\w@%+=:,./\\-]+$")


class MissingParameter(ValueError):
    """A required argument or option was not given."""


class InvalidValue(ValueError):
    """A value does not fit its parameter's type."""


def base_argv(ctx: Context) -> list:
    """[interpreter, pipeline.py, --config, <path>]: the part every command shares."""
    return [ctx.python, str(ctx.pipeline_script), "--config", str(ctx.config_path)]


def _empty(value) -> bool:
    return value is None or value == "" or value == [] or value == ()


def _same_as_default(param, text: str) -> bool:
    if param.default is None:
        return False
    if param.type in ("float", "int"):
        try:
            return float(text) == float(param.default)
        except ValueError:
            return False
    return text == str(param.default)


def _text(param, value) -> str:
    text = str(value).strip() if param.type != "path" else str(value)
    if param.type == "float":
        try:
            float(text)
        except ValueError:
            raise InvalidValue(f"{param.name}: {text!r} is not a number") from None
    elif param.type == "int":
        try:
            int(text)
        except ValueError:
            raise InvalidValue(f"{param.name}: {text!r} is not a whole number") from None
    elif param.type == "choice" and param.choices and text not in param.choices:
        raise InvalidValue(f"{param.name}: {text!r} is not one of {', '.join(param.choices)}")
    return text


def _flag_name(param) -> str:
    longs = [o for o in param.opts if o.startswith("--")]
    return longs[0] if longs else param.opts[0]


def argv_for(ctx: Context, spec: CommandSpec, values: dict, *, keep_typed: bool = False) -> list:
    """The full argv for `spec` with the form's `values` (keyed by parameter name).

    Arguments come first in declaration order, then options, as in the cheat sheet.
    An option equal to its default, an unticked flag and an empty value are left out,
    so the result is what an operator would type by hand. With `keep_typed`, a number the
    operator did type stays in even if it equals the default (the print-prep screen uses
    this so a bleed of 3 is visible in the command, as in the cheat sheet's examples)."""
    positionals, options = [], []
    for param in spec.params:
        value = values.get(param.name)
        if param.kind == "flag":
            if value:
                options.append(_flag_name(param))
            continue
        if _empty(value):
            if param.required:
                label = param.name.upper() if param.kind == "argument" else param.opts[0]
                raise MissingParameter(f"Missing required {param.kind}: {label}")
            continue
        items = list(value) if isinstance(value, (list, tuple)) else [value]
        texts = [_text(param, item) for item in items]
        if param.kind == "argument":
            positionals.extend(texts)
        elif (len(texts) > 1 or param.multiple or (keep_typed and param.type in ("float", "int"))
              or not _same_as_default(param, texts[0])):
            for text in texts:
                options += [_flag_name(param), text]

    tail = positionals + options
    if any(p.startswith("-") for p in positionals):
        tail = options + ["--"] + positionals  # a file called "-x" must not be read as an option
    return base_argv(ctx) + spec.path + tail


def _quote(arg: str) -> str:
    if arg and _SAFE_WORD.match(arg):
        return arg
    if sys.platform == "win32":
        # PowerShell: double quotes expand $ and `, so use single quotes for those.
        if any(c in arg for c in '$`"'):
            return "'" + arg.replace("'", "''") + "'"
        return '"' + arg + '"'
    return "'" + arg.replace("'", "'\"'\"'") + "'"


def display(argv: list) -> str:
    """`argv` as the line to type: `uv run python pipeline.py ...`. The interpreter and
    script are replaced by that prefix, and `--config` is dropped when it names the
    repository's own config.yaml."""
    script = Path(argv[1])
    rest = list(argv[2:])
    if rest[:1] == ["--config"] and len(rest) >= 2:
        try:
            default = Path(rest[1]).resolve() == (script.resolve().parent / DEFAULT_CONFIG).resolve()
        except OSError:
            default = False
        if default:
            rest = rest[2:]
    return " ".join([DISPLAY_PREFIX] + [_quote(a) for a in rest])
