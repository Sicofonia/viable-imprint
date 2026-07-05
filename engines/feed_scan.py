"""System 4's `scan` task: pulls new items from a curated watchlist of
external sources (systems/s4/sources.yaml) — no LLM call, no crawling. Three
source shapes, each handled on its own terms:

- `feeds`: RSS/Atom feeds, diffed against `_state.yaml` so only items not
  seen in a previous run are written out.
- `gutenberg_catalog`: Project Gutenberg's own bulk catalog CSV (their
  sanctioned bulk-access mechanism, updated weekly), filtered by subject
  keywords and diffed the same way, keyed on Text# instead of a feed GUID.
- `catalog_reference`: a small set of static pages (the imprint's own
  catalog) fetched fresh every run with no diffing — this isn't "news," it's
  context the `briefing` task uses to tell a genuine gap from something
  already published.

See docs/adr/003-system-4-strategic-intelligence.md for the full reasoning.
"""
import csv
import io
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import click
import feedparser
import httpx
import yaml

from lib import manifest

CLI_ARG = "none"

_USER_AGENT = "ViableImprint-S4Scan/0.1 (+https://github.com/Sicofonia/viable-imprint)"
_TIMEOUT = 30.0


def run(root: Path, system: str, output_name: str, config: dict, *, sources_config: str) -> Path:
    sources_path = Path(sources_config)
    if not sources_path.exists():
        raise click.ClickException(
            f"Sources config not found: {sources_path}\n"
            "Copy systems/s4/sources.example.yaml to that path and adjust it to your own watchlist."
        )
    with open(sources_path, encoding="utf-8") as f:
        sources = yaml.safe_load(f) or {}

    scan_dir = root / system / output_name
    state_path = scan_dir / "_state.yaml"
    state = _load_state(state_path)

    run_dir = scan_dir / date.today().isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)

    combined_sections = []

    for feed_cfg in sources.get("feeds", []):
        text, new_ids = _scan_feed(feed_cfg, state.get(feed_cfg["name"], []))
        state[feed_cfg["name"]] = new_ids
        (run_dir / f"{feed_cfg['name']}.txt").write_text(text, encoding="utf-8")
        combined_sections.append(_wrap_source(feed_cfg["label"], "nuevo desde el último scan", text))

    gutenberg_cfg = sources.get("gutenberg_catalog")
    if gutenberg_cfg:
        text, new_ids = _scan_gutenberg(gutenberg_cfg, state.get("gutenberg", []))
        state["gutenberg"] = new_ids
        (run_dir / "gutenberg.txt").write_text(text, encoding="utf-8")
        combined_sections.append(
            _wrap_source(
                "Project Gutenberg — nuevos títulos que coinciden con el género",
                "nuevo desde el último scan",
                text,
            )
        )

    for ref_cfg in sources.get("catalog_reference", []):
        text = _fetch_catalog_reference(ref_cfg)
        (run_dir / f"{ref_cfg['name']}.txt").write_text(text, encoding="utf-8")
        combined_sections.append(
            _wrap_source(ref_cfg["label"], "REFERENCIA -- catalogo ya publicado, no son noticias", text)
        )

    combined_path = run_dir / "combined.txt"
    combined_path.write_text("\n\n".join(combined_sections), encoding="utf-8")

    _save_state(state_path, state)

    manifest.update(
        root,
        **{f"{output_name}_latest": str(combined_path.relative_to(root))},
    )

    click.echo(f"Saved: {combined_path}")
    return combined_path


def _wrap_source(label: str, status: str, text: str) -> str:
    """Delimit a source's content with XML-style tags rather than a
    Markdown-heading-like "=== LABEL ===" marker. `briefing`'s prompt found
    that the latter visually "primed" the model to paraphrase its own output
    headings in the same descriptive style, instead of reproducing the fixed
    headings literally (same class of finding as the `brief` task's switch
    to tags, ADR 002 Decision point 7) — tags read as structural, not as
    prose worth imitating.
    """
    return f'<fuente nombre="{label}" estado="{status}">\n{text}\n</fuente>'


# ---------------------------------------------------------------------------
# State (last-seen items, persists across runs)
# ---------------------------------------------------------------------------

def _load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    try:
        with open(state_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError:
        click.echo(f"  Warning: {state_path} is corrupted — treating every source as if seen for the first time.")
        return {}


def _save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        yaml.dump(state, f, default_flow_style=False, allow_unicode=True)


# ---------------------------------------------------------------------------
# RSS/Atom feeds
# ---------------------------------------------------------------------------

def _scan_feed(feed_cfg: dict, seen_ids: list) -> tuple:
    name, label, feed_url = feed_cfg["name"], feed_cfg["label"], feed_cfg["feed_url"]
    click.echo(f"  Fetching {label}...")

    try:
        response = httpx.get(feed_url, headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: one bad source must not fail the whole scan
        click.echo(f"  Warning: could not fetch {label} ({exc}); skipping this source for this run.")
        return "Fuente no disponible en este scan (error de red o del feed).", seen_ids

    seen = set(seen_ids)
    new_entries = [e for e in parsed.entries if (e.get("id") or e.get("link")) not in seen]

    if not new_entries:
        return "Sin elementos nuevos desde el último scan.", list(seen)

    lines = []
    for entry in new_entries:
        entry_id = entry.get("id") or entry.get("link")
        seen.add(entry_id)
        lines.append(f"- {entry.get('title', '(sin título)')}")
        if entry.get("published"):
            lines.append(f"  Fecha: {entry.published}")
        if entry.get("link"):
            lines.append(f"  Enlace: {entry.link}")
        summary = (entry.get("summary") or "").strip()
        if summary:
            lines.append(f"  {_html_to_text(summary)[:600]}")
        lines.append("")

    return "\n".join(lines).strip(), list(seen)


# ---------------------------------------------------------------------------
# Project Gutenberg bulk catalog
# ---------------------------------------------------------------------------

def _scan_gutenberg(cfg: dict, seen_ids: list) -> tuple:
    catalog_url = cfg["catalog_url"]
    keywords = [k.lower() for k in cfg.get("subject_keywords", [])]
    click.echo("  Fetching Project Gutenberg catalog (this file is several MB, may take a moment)...")

    try:
        response = httpx.get(catalog_url, headers={"User-Agent": _USER_AGENT}, timeout=120.0, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        click.echo(f"  Warning: could not fetch the Gutenberg catalog ({exc}); skipping this source for this run.")
        return "Fuente no disponible en este scan (error de red).", seen_ids

    reader = csv.DictReader(io.StringIO(response.content.decode("utf-8", errors="replace")))
    seen = set(seen_ids)
    new_rows = []

    for row in reader:
        text_id = row.get("Text#")
        if not text_id or text_id in seen:
            continue
        haystack = " ".join(
            (row.get("Subjects") or "", row.get("Bookshelves") or "", row.get("LoCC") or "")
        ).lower()
        if any(keyword in haystack for keyword in keywords):
            new_rows.append(row)
        # Every row seen this run counts as "seen" even if it didn't match —
        # otherwise a future keyword-list change would resurface the entire
        # backlog of non-matching rows as if they were new.
        seen.add(text_id)

    if not new_rows:
        return "Sin títulos nuevos que coincidan con las materias configuradas.", list(seen)

    # Defensive cap, independent of how well-tuned subject_keywords is: a
    # cold start (empty state) or an accidentally too-broad keyword matches
    # the *entire* historical backlog at once, which would otherwise flood
    # `briefing`'s single LLM call. Text# increases with when a book was
    # added to Gutenberg, so sorting by it descending keeps the most
    # recently added matches, which is what "new listings" actually means.
    max_new_items = cfg.get("max_new_items", 50)
    new_rows.sort(key=lambda r: int(r.get("Text#", 0)), reverse=True)
    truncated = len(new_rows) - max_new_items
    new_rows = new_rows[:max_new_items]

    lines = []
    if truncated > 0:
        lines.append(
            f"(Se encontraron {truncated} coincidencias adicionales no incluidas aquí — "
            f"revisa subject_keywords en sources.yaml si esto ocurre de forma habitual.)"
        )
        lines.append("")
    for row in new_rows:
        lines.append(f"- {row.get('Title', '(sin título)')} — {row.get('Authors', 'autor desconocido')}")
        lines.append(f"  Gutenberg #{row.get('Text#')} — https://www.gutenberg.org/ebooks/{row.get('Text#')}")
        if row.get("Subjects"):
            lines.append(f"  Materias: {row.get('Subjects')}")
        if row.get("Issued"):
            lines.append(f"  Catalogado: {row.get('Issued')}")
        lines.append("")

    return "\n".join(lines).strip(), list(seen)


# ---------------------------------------------------------------------------
# Static catalog reference (the imprint's own site — no diffing, always fresh)
# ---------------------------------------------------------------------------

def _fetch_catalog_reference(ref_cfg: dict) -> str:
    click.echo(f"  Fetching {ref_cfg['label']} (reference, not diffed)...")
    pages = []
    for url in ref_cfg.get("urls", []):
        try:
            response = httpx.get(url, headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT, follow_redirects=True)
            response.raise_for_status()
            pages.append(_html_to_text(response.text))
        except httpx.HTTPError as exc:
            click.echo(f"  Warning: could not fetch {url} ({exc}); skipping this page.")
    return "\n\n".join(pages) if pages else "Referencia no disponible en este scan (error de red)."


# ---------------------------------------------------------------------------
# Minimal HTML -> text (stdlib only; readability for an LLM prompt, not
# precise structured extraction)
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer"):
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self.parts.append(text)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return "\n".join(parser.parts)
