"""ODT generation by appending styled content to an existing template.

Loads a publisher-provided .odt template (page setup, margins, and named
paragraph styles already defined — chapter heading, first paragraph, body
text, etc.) and appends the manuscript content after whatever the template
already contains. The template owns layout; this module only adds content,
using the template's own named styles rather than inventing new ones.

Chapter structure is detected directly from the text rather than via new
markup tags: a short, fully-uppercase line containing a roman numeral or
digit (e.g. "CAPÍTULO I") is treated as the chapter-number line, and an
immediately following short uppercase line is treated as the chapter title.
The paragraph right after a detected heading gets the "first paragraph"
style; everything else gets "body".

Inline markup ([i]...[/i], [sc]...[/sc], [FN: ...[/FN]]) is left as literal
visible text — applying that formatting is a deliberate manual step.
"""
import re
from pathlib import Path

import click
from odf.opendocument import load
from odf.style import Style, TextProperties
from odf.text import P, Span

_ROMAN_OR_DIGIT_TOKEN_RE = re.compile(r"\b([IVXLCDM]+|\d+)\b")


def write(text: str, output_path: Path, template_path: Path, style_map: dict) -> None:
    if not template_path.exists():
        raise click.ClickException(f"Template not found: {template_path}")

    doc = load(str(template_path))
    styles = _resolve_styles(doc, style_map)

    blocks = [b for b in text.split("\n\n") if b.strip()]
    next_is_first = False
    i = 0
    while i < len(blocks):
        block = blocks[i]
        lines = [ln for ln in block.split("\n") if ln.strip()]

        if lines and _is_chapter_number_line(lines[0]):
            _append_paragraph(doc, lines[0].strip(), styles["chapter_number"])

            if len(lines) > 1 and _is_title_like_line(lines[1]):
                _append_paragraph(doc, lines[1].strip(), styles["chapter_title"])
            elif i + 1 < len(blocks) and _is_title_like_line(blocks[i + 1].strip()):
                # Title landed in its own \n\n-separated block instead of
                # sharing a block with the chapter number.
                i += 1
                _append_paragraph(doc, blocks[i].strip(), styles["chapter_title"])

            next_is_first = True
        else:
            if next_is_first:
                _append_first_paragraph(doc, block, styles["first_paragraph"])
            else:
                _append_paragraph(doc, block, styles["body"])
            next_is_first = False

        i += 1

    doc.save(str(output_path))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _append_paragraph(doc, text: str, style_name: str) -> None:
    p = P(stylename=style_name)
    p.addText(text)
    doc.text.addElement(p)


def _append_first_paragraph(doc, text: str, style_name: str) -> None:
    """Append a first-paragraph block with the opening word in small capitals.

    Spanish (and broader European) book typography sets the first word of each
    chapter's opening paragraph in versalitas (small capitals). The rest of the
    paragraph uses the paragraph's normal character formatting.
    """
    versalitas = _ensure_versalitas_style(doc)
    p = P(stylename=style_name)
    match = re.match(r"(\S+)(.*)", text, re.DOTALL)
    if match:
        first_word, rest = match.group(1), match.group(2)
        span = Span(stylename=versalitas)
        span.addText(first_word)
        p.addElement(span)
        if rest:
            p.addText(rest)
    else:
        p.addText(text)
    doc.text.addElement(p)


def _ensure_versalitas_style(doc) -> str:
    """Inject an automatic character style for small capitals, idempotent."""
    _NAME = "_vi_versalitas"
    for el in doc.automaticstyles.childNodes:
        if getattr(el, "getAttribute", None) and el.getAttribute("name") == _NAME:
            return _NAME
    style = Style(name=_NAME, family="text")
    style.addElement(TextProperties(fontvariant="small-caps"))
    doc.automaticstyles.addElement(style)
    return _NAME


def _resolve_styles(doc, style_map: dict) -> dict:
    """Resolve configured style names to the internal style:name ODF requires
    for document references.

    LibreOffice keeps two names per style: the internal style:name (a valid
    XML identifier — spaces become "_20_", etc.) and a separate
    style:display-name, which is the human-readable name shown in the
    LibreOffice Styles panel and the one publishers actually configure.
    Document content can only reference styles by their internal name, so
    we look up each configured (display) name and translate it.
    """
    by_internal_name = {}
    by_display_name = {}
    for el in doc.getElementsByType(Style):
        internal_name = el.getAttribute("name")
        if not internal_name:
            continue
        by_internal_name[internal_name] = internal_name
        display_name = el.getAttribute("displayname")
        if display_name:
            by_display_name[display_name] = internal_name

    resolved = {}
    missing = {}
    for role, configured_name in style_map.items():
        if configured_name in by_display_name:
            resolved[role] = by_display_name[configured_name]
        elif configured_name in by_internal_name:
            resolved[role] = by_internal_name[configured_name]
        else:
            missing[role] = configured_name

    if missing:
        missing_lines = "\n".join(f"  - {role}: '{name}'" for role, name in missing.items())
        available = ", ".join(sorted(set(by_display_name) | set(by_internal_name))) or "(none found)"
        raise click.ClickException(
            "Style(s) referenced in your style config were not found in the template:\n"
            f"{missing_lines}\n\nStyles available in the template: {available}"
        )

    return resolved


def _is_chapter_number_line(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 40 or not line.isupper():
        return False
    return bool(_ROMAN_OR_DIGIT_TOKEN_RE.search(line))


def _is_title_like_line(line: str) -> bool:
    line = line.strip()
    return bool(line) and len(line) < 100 and line.isupper()
