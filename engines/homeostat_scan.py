"""System 5's `homeostat-scan` task: gathers System 3's portfolio snapshot,
System 4's latest briefing, and the homeostat decision log into one
tag-delimited file for `homeostat` (an `llm_text` task) to synthesize into a
tensions-and-tradeoffs narrative. No LLM call here — numbers are always
computed from structured data, never generated. See
docs/adr/007-system5-homeostat-dashboard.md.
"""
from datetime import date
from pathlib import Path

import click

from lib import dashboard, homeostat, manifest

CLI_ARG = "none"


def run(root: Path, system: str, output_name: str, config: dict) -> Path:
    books_dir = Path(config.get("books_dir", "books"))
    portfolio = dashboard.portfolio_summary(books_dir)
    briefing_text, briefing_label = homeostat.latest_s4_briefing(config)
    decisions = homeostat.load_decisions(root)

    combined = "\n\n".join([
        _wrap("Resumen de System 3 (rendimiento)", "generado en este scan", _render_portfolio(portfolio)),
        _wrap("Ultimo briefing de System 4 (inteligencia estrategica)",
              f"del {briefing_label}" if briefing_label else "no disponible", briefing_text),
        _wrap("Decisiones previas del homeostato", "historico", _render_decisions(decisions)),
    ])

    run_dir = root / system / output_name / date.today().isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)
    combined_path = run_dir / "combined.txt"
    combined_path.write_text(combined, encoding="utf-8")

    manifest.update(root, **{f"{output_name}_latest": str(combined_path.relative_to(root))})

    click.echo(f"Saved: {combined_path}")
    return combined_path


def _wrap(nombre: str, estado: str, text: str) -> str:
    """Same `<fuente>` delimiter convention `feed_scan.py` established (ADR
    003 point 10) — proven to avoid priming the model into inventing
    similarly-styled headings of its own in its output.
    """
    return f'<fuente nombre="{nombre}" estado="{estado}">\n{text}\n</fuente>'


def _render_portfolio(summaries: list) -> str:
    if not summaries:
        return "Sin libros en el portafolio todavia."
    lines = []
    for s in summaries:
        cost = f"${s['cost_usd']:.2f}" if s["cost_usd"] is not None else "sin coste conocido"
        lines.append(
            f"- {s['slug']}: {s['tasks_done']}/{s['tasks_total']} tareas completadas, "
            f"coste API {cost}, tiempo de computo {s['duration_seconds']:.0f}s"
        )
    return "\n".join(lines)


def _render_decisions(decisions: list) -> str:
    if not decisions:
        return "Sin decisiones registradas todavia."
    lines = [
        f"- {d.get('date', '?')}: tension - {d.get('tension', '')}; decision - {d.get('decision', '')}"
        for d in sorted(decisions, key=lambda x: x.get("date", ""), reverse=True)
    ]
    return "\n".join(lines)
