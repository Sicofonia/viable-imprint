"""Renders a `CommandSpec` as a form (ADR 023, Decision 3).

A text field for text and numbers, a file picker button for a path that must
already exist, a dropdown for a `click.Choice`, a checkbox for a flag. The form
knows nothing about any particular command: it is generated from the Click tree,
so a command added tomorrow gets a form with no UI change.
"""
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from tui.screens.file_picker import FilePickerScreen
from tui.services.command_model import CommandSpec, ParamSpec


# Textual renamed Select's "nothing chosen" sentinel (BLANK became NULL, and BLANK is now just False).
NO_SELECTION = Select.NULL if hasattr(Select, "NULL") else Select.BLANK


def field_id(param: ParamSpec) -> str:
    return f"param-{param.name}"


def caption(param: ParamSpec) -> str:
    name = param.name.upper() if param.kind == "argument" else " / ".join(param.opts)
    return f"{name}{' *' if param.required else ''}"


def hint(param: ParamSpec) -> str:
    bits = [param.help] if param.help else []
    if param.default not in (None, False, ""):
        bits.append(f"default: {param.default}")
    return "  ".join(bits)


class ParamForm(Vertical):
    class Changed(Message):
        """Any field changed: the owner should rebuild the command line. `form` is the
        ParamForm and `name` the parameter whose field changed."""

        def __init__(self, form: "ParamForm", name: str):
            super().__init__()
            self.form = form
            self.name = name

    DEFAULT_CSS = """
    ParamForm { height: auto; }
    ParamForm .param { height: auto; margin-bottom: 1; }
    ParamForm .param-caption { text-style: bold; }
    ParamForm .param-hint { color: $text-muted; }
    ParamForm .param-row { height: auto; }
    ParamForm .param-row Input { width: 1fr; }
    """

    def __init__(self, browse_start: Path = None, **kwargs):
        super().__init__(**kwargs)
        self.spec: CommandSpec = None
        self.browse_start = Path(browse_start) if browse_start else Path.cwd()

    async def set_spec(self, spec: CommandSpec) -> None:
        self.spec = spec
        await self.remove_children()
        await self.mount(*[self._field(p) for p in spec.params])

    def _field(self, param: ParamSpec) -> Vertical:
        parts = [Label(caption(param), classes="param-caption", markup=False)]
        if param.kind == "flag":
            parts.append(Checkbox(param.help or "enable", id=field_id(param)))
        elif param.type == "choice":
            options = [(choice, choice) for choice in param.choices]
            parts.append(Select(options, id=field_id(param), allow_blank=not param.required,
                                value=param.default if param.default in param.choices else NO_SELECTION))
        else:
            kind = {"float": "number", "int": "integer"}.get(param.type, "text")
            box = Input(id=field_id(param), type=kind, placeholder=hint(param) if param.kind != "flag" else "")
            if param.type == "path" and param.path_must_exist:
                parts.append(Horizontal(box, Button("Browse", id=f"browse-{param.name}", classes="browse"),
                                        classes="param-row"))
            else:
                parts.append(box)
        if param.kind != "flag" and hint(param):
            parts.append(Static(hint(param), classes="param-hint", markup=False))
        return Vertical(*parts, classes="param")

    # -- reading and writing values ---------------------------------------

    def values(self) -> dict:
        found = {}
        for param in self.spec.params if self.spec else []:
            widget = self.query_one(f"#{field_id(param)}")
            if isinstance(widget, Checkbox):
                found[param.name] = widget.value
            elif isinstance(widget, Select):
                found[param.name] = None if widget.value == NO_SELECTION else widget.value
            else:
                text = widget.value.strip()
                found[param.name] = (text.split() if param.multiple else text) or None
        return found

    def set_value(self, name: str, value) -> None:
        widget = self.query_one(f"#param-{name}")
        if isinstance(widget, Checkbox):
            widget.value = bool(value)
        elif isinstance(widget, Select):
            widget.value = value if value is not None else NO_SELECTION
        else:
            widget.value = "" if value is None else str(value)

    # -- events -------------------------------------------------------------

    def _announce(self, widget) -> None:
        self.post_message(self.Changed(self, (widget.id or "")[len("param-"):]))

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._announce(event.input)

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        self._announce(event.select)

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        event.stop()
        self._announce(event.checkbox)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if not (event.button.id or "").startswith("browse-"):
            return
        event.stop()
        name = event.button.id[len("browse-"):]
        current = self.query_one(f"#param-{name}", Input).value.strip()
        start = Path(current).parent if current and Path(current).parent.is_dir() else self.browse_start

        def chosen(path):
            if path:
                self.set_value(name, path)

        self.app.push_screen(FilePickerScreen(start, title=f"Choose {name.replace('_', ' ')}"), chosen)
