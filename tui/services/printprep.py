"""Arithmetic and defaults for the print-prep screen (ADR 023, T3.1; ADR 019).
Plain Python, no Textual.

The cover PDF the operator exports from LibreOffice must be exactly the trim width
plus spine plus a bleed on each side, by the trim height plus a bleed above and
below; `s1c set-cover` takes the width *without* the spine and adds it itself. Showing
the expected size while the numbers are typed catches a wrong export size before the
command runs.
"""
from pathlib import Path

DEFAULT_BLEED = 3.0
DEFAULT_SPINE = 0.0
DEFAULT_TRIM_TOLERANCE = 0.4

WORKFLOW_NOTE = (
    "Workflow: s1b format -> .odt -> review and finish the layout, then export the PDF from LibreOffice by "
    "hand -> the forms below -> upload to the print platform by hand. Nothing here touches the run-state "
    "ledger and s2 run never does this step.\n"
    "LibreOffice only writes /MediaBox; print vendors (Bibliomanager, IngramSpark) want explicit /TrimBox "
    "and /BleedBox. Sizes are millimetres unless you pick pt.\n"
    "Interior: use your .odt page size (Format > Page Style in LibreOffice). Cover: a separate, wider PDF "
    "(trim width x 2 + spine + bleed on each side); it does not match the .odt."
)


def default_output(input_path: str) -> str:
    """`<input>_print.pdf`, next to the input. Never equal to the input, whatever it is called."""
    path = Path(input_path)
    return str(path.with_name(f"{path.stem}_print{path.suffix or '.pdf'}"))


def expected_cover_size(trim_w: float, trim_h: float, spine_w: float = DEFAULT_SPINE,
                        bleed: float = DEFAULT_BLEED) -> tuple:
    """(width, height) of the cover PDF the export must produce:
    (trim_w + spine_w + 2*bleed) x (trim_h + 2*bleed)."""
    return trim_w + spine_w + 2 * bleed, trim_h + 2 * bleed


def _number(value) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def describe_size(width: float, height: float, unit: str = "mm") -> str:
    return f"{_number(width)} x {_number(height)} {unit}"


def _parse(value, default=None):
    if value in (None, ""):
        return default, None
    try:
        return float(str(value).strip()), None
    except ValueError:
        return None, f"{value!r} is not a number"


def cover_preview(values: dict) -> str:
    """The sentence under the cover form, from the form's current text values."""
    unit = values.get("unit") or "mm"
    parsed = {}
    for key, default in (("trim_w", None), ("trim_h", None), ("spine_w", DEFAULT_SPINE), ("bleed", DEFAULT_BLEED)):
        number, problem = _parse(values.get(key), default)
        if problem:
            return f"Cannot compute the expected size: {problem}."
        parsed[key] = number
    if parsed["trim_w"] is None or parsed["trim_h"] is None:
        return "Enter the cover width (without the spine) and the height to see the PDF page size the export must have."
    width, height = expected_cover_size(parsed["trim_w"], parsed["trim_h"], parsed["spine_w"], parsed["bleed"])
    return (f"Export the cover PDF at {describe_size(width, height, unit)}: "
            f"({_number(parsed['trim_w'])} + {_number(parsed['spine_w'])} spine + 2 x {_number(parsed['bleed'])} bleed) "
            f"x ({_number(parsed['trim_h'])} + 2 x {_number(parsed['bleed'])} bleed).")
