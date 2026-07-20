"""System 1D's `newsletter-scan` task: gathers books with activity this
calendar month, this month's human-written notes (if any), and the two
non-repetition tracking lists into one tag-delimited file for `newsletter`
(an `llm_text` task) to draft the newsletter from. No LLM call here. See
docs/adr/008-system1d-newsletter.md.
"""
from datetime import date
from pathlib import Path

import click
import yaml

from lib import dashboard, manifest

CLI_ARG = "none"


def run(root: Path, system: str, output_name: str, config: dict) -> Path:
    books_dir = Path(config.get("books_dir", "books"))
    this_month = date.today().isoformat()[:7]  # "YYYY-MM"

    active_books = [
        s for s in dashboard.portfolio_summary(books_dir)
        if s["span_end"] and s["span_end"][:7] == this_month
    ]
    notes_text, notes_status = _monthly_notes(root, this_month)
    explorers = _load_list(root / "featured_explorers.yaml")
    dishes = _load_list(root / "featured_dishes.yaml")

    combined = "\n\n".join([
        _wrap("Actividad de este mes en produccion", "generado en este scan", _render_active_books(active_books)),
        _wrap("Notas del mes proporcionadas por el Director", notes_status, notes_text),
        _wrap("Exploradores ya destacados en ediciones anteriores", "historico",
              _render_list(explorers, "Sin exploradores destacados todavia.")),
        _wrap("Platos ya destacados en ediciones anteriores", "historico",
              _render_list(dishes, "Sin platos destacados todavia.")),
    ])

    run_dir = root / system / output_name / date.today().isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)
    combined_path = run_dir / "combined.txt"
    combined_path.write_text(combined, encoding="utf-8")

    manifest.update(root, **{f"{output_name}_latest": str(combined_path.relative_to(root))})

    click.echo(f"Saved: {combined_path}")
    return combined_path


def _wrap(nombre: str, estado: str, text: str) -> str:
    """Same `<fuente>` delimiter convention `feed_scan.py`/`homeostat_scan.py`
    use (ADR 003 point 10) — avoids priming the model into inventing
    similarly-styled headings of its own in its output.
    """
    return f'<fuente nombre="{nombre}" estado="{estado}">\n{text}\n</fuente>'


def _monthly_notes(root: Path, this_month: str) -> tuple:
    notes_dir = root / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    notes_path = notes_dir / f"{this_month}.txt"
    if not notes_path.exists():
        click.echo(f"  Warning: no notes file for this month ({notes_path}) — continuing without it.")
        return "Sin notas proporcionadas para este mes.", "no disponible"
    text = notes_path.read_text(encoding="utf-8").strip()
    if not text:
        return "Sin notas proporcionadas para este mes.", "vacio"
    return text, f"del {this_month}"


def _render_active_books(active_books: list) -> str:
    if not active_books:
        return "Sin actividad de produccion registrada este mes."
    lines = [f"- {s['slug']}: {s['tasks_done']}/{s['tasks_total']} tareas completadas" for s in active_books]
    return "\n".join(lines)


def _load_list(path: Path) -> list:
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or []
    except yaml.YAMLError:
        click.echo(f"  Warning: {path} is corrupted — treating this list as empty.")
        return []


def _render_list(entries: list, empty_message: str) -> str:
    if not entries:
        return empty_message
    return "\n".join(f"- {e.get('name', '?')} (destacado el {e.get('date_featured', '?')})" for e in entries)
