"""System 5's `homeostat-render` task: renders the final, self-contained
`homeostat.html` — the actual dashboard. Code only, no LLM call: the
portfolio table and cost chart are always computed from structured data,
never generated. Converts the two Markdown documents it embeds (the
`homeostat` narrative and the latest S4 briefing) with a small bespoke
converter for the narrow Markdown subset this project's prompts actually
produce (headings, paragraphs, bullet lists) — a general Markdown library
would cover formatting these prompts never generate. See
docs/adr/007-system5-homeostat-dashboard.md, point 4.
"""
import html
import re
from datetime import date
from pathlib import Path

import click

from lib import dashboard, homeostat, paths

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*]\s+(.*)$")


def run(input_file: Path, root: Path, system: str, output_name: str, config: dict) -> Path:
    tensions_md = input_file.read_text(encoding="utf-8")
    books_dir = Path(config.get("books_dir", "books"))
    portfolio = dashboard.portfolio_summary(books_dir)
    briefing_text, briefing_label = homeostat.latest_s4_briefing(config)
    decisions = homeostat.load_decisions(root)

    page = _render_page(tensions_md, portfolio, briefing_text, briefing_label, decisions)

    output_dir = paths.stage_output_dir(input_file, root, system, output_name)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / (input_file.stem + ".html")
    output_file.write_text(page, encoding="utf-8")

    click.echo(f"Saved: {output_file}")
    return output_file


def _render_page(tensions_md: str, portfolio: list, briefing_text: str,
                  briefing_label: str, decisions: list) -> str:
    briefing_meta = f"Fecha: {briefing_label}" if briefing_label else "Sin briefing generado todavia."
    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Homeostato -- Sistemas 3/4/5</title>
<style>{_CSS}</style>
</head>
<body>
<h1>Homeostato -- confrontacion System 3 / System 4</h1>
<p class="meta">Generado el {date.today().isoformat()}</p>

<section>
<h2>Resumen de System 3 (rendimiento)</h2>
{_render_portfolio_table(portfolio)}
{_render_cost_chart(portfolio)}
</section>

<section>
<h2>Tensiones y disyuntivas (System 5)</h2>
{_markdown_lite_to_html(tensions_md)}
</section>

<section>
<h2>Ultimo briefing de System 4</h2>
<p class="meta">{briefing_meta}</p>
{_markdown_lite_to_html(briefing_text)}
</section>

<section>
<h2>Historial de decisiones</h2>
{_render_decisions_table(decisions)}
</section>
</body>
</html>
"""


def _cost_str(cost_usd) -> str:
    if cost_usd is None:
        return "-"
    return f"${cost_usd:.6f}" if cost_usd < 0.01 else f"${cost_usd:.3f}"


def _render_portfolio_table(portfolio: list) -> str:
    if not portfolio:
        return "<p>Sin libros en el portafolio todavia.</p>"
    rows = "".join(
        f"<tr><td>{html.escape(s['slug'])}</td><td>{s['tasks_done']}/{s['tasks_total']}</td>"
        f"<td>{_cost_str(s['cost_usd'])}</td><td>{s['duration_seconds']:.0f}s</td></tr>"
        for s in portfolio
    )
    return (
        "<table><thead><tr><th>Libro</th><th>Tareas</th><th>Coste API</th>"
        f"<th>Tiempo de computo</th></tr></thead><tbody>{rows}</tbody></table>"
    )


def _render_cost_chart(portfolio: list) -> str:
    known = [s for s in portfolio if s["cost_usd"] is not None]
    if not known:
        return ""
    max_cost = max(s["cost_usd"] for s in known) or 1
    bar_height, gap, width, label_width = 24, 8, 500, 150
    bars = []
    for i, s in enumerate(known):
        y = i * (bar_height + gap)
        bar_len = (s["cost_usd"] / max_cost) * (width - label_width - 60)
        bars.append(
            f'<text x="0" y="{y + bar_height * 0.7:.1f}" class="label">{html.escape(s["slug"])}</text>'
            f'<rect x="{label_width}" y="{y}" width="{bar_len:.1f}" height="{bar_height}" class="bar" />'
            f'<text x="{label_width + bar_len + 6:.1f}" y="{y + bar_height * 0.7:.1f}" class="value">'
            f'{_cost_str(s["cost_usd"])}</text>'
        )
    svg_height = len(known) * (bar_height + gap)
    return f'<svg viewBox="0 0 {width} {svg_height}" class="chart">{"".join(bars)}</svg>'


def _render_decisions_table(decisions: list) -> str:
    if not decisions:
        return "<p>Sin decisiones registradas todavia.</p>"
    rows = "".join(
        f"<tr><td>{html.escape(d.get('date', ''))}</td>"
        f"<td>{html.escape(d.get('tension', ''))}</td>"
        f"<td>{html.escape(d.get('decision', ''))}</td></tr>"
        for d in sorted(decisions, key=lambda x: x.get("date", ""), reverse=True)
    )
    return (
        "<table><thead><tr><th>Fecha</th><th>Tension</th><th>Decision</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _markdown_lite_to_html(text: str) -> str:
    """Converts a narrow, known subset of Markdown (headings, paragraphs,
    bullet lists) to HTML. Heading level is offset by +2 so a prompt's `##`
    (this project's standard top-level section marker) nests as <h4>,
    subordinate to the page's own <h2> section headings — and, for the one
    embedded document with a genuine `#` title (`s4 briefing`), that title
    lands at <h3>, one level under the page's own section header rather than
    level with it.
    """
    html_parts, para_buffer, list_buffer = [], [], []

    def flush_para():
        if para_buffer:
            html_parts.append(f"<p>{html.escape(' '.join(para_buffer))}</p>")
            para_buffer.clear()

    def flush_list():
        if list_buffer:
            items = "".join(f"<li>{html.escape(item)}</li>" for item in list_buffer)
            html_parts.append(f"<ul>{items}</ul>")
            list_buffer.clear()

    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line:
            flush_para()
            flush_list()
            continue

        heading_match = _HEADING_RE.match(line)
        if heading_match:
            flush_para()
            flush_list()
            level = min(len(heading_match.group(1)) + 2, 6)
            html_parts.append(f"<h{level}>{html.escape(heading_match.group(2))}</h{level}>")
            continue

        bullet_match = _BULLET_RE.match(line)
        if bullet_match:
            flush_para()
            list_buffer.append(bullet_match.group(1))
            continue

        flush_list()
        para_buffer.append(line)

    flush_para()
    flush_list()
    return "\n".join(html_parts)


_CSS = """
body { font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; max-width: 900px;
       margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; line-height: 1.5; }
h1 { font-size: 1.6rem; border-bottom: 2px solid #2b6cb0; padding-bottom: 0.5rem; }
h2 { font-size: 1.3rem; margin-top: 2rem; color: #2b6cb0; }
h3, h4, h5, h6 { font-size: 1.05rem; margin-top: 1.2rem; }
.meta { color: #666; font-size: 0.9rem; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
th, td { border: 1px solid #ddd; padding: 0.4rem 0.6rem; text-align: left; font-size: 0.95rem; }
th { background: #f4f6f8; }
.chart { width: 100%; max-width: 500px; margin: 1rem 0; }
.chart .bar { fill: #2b6cb0; }
.chart .label, .chart .value { font-size: 11px; fill: #1a1a1a; }
section { margin-bottom: 2.5rem; }
"""
