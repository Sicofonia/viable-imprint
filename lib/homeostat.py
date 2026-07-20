"""Shared data access for System 5's homeostat pipeline (ADR 007) — used by
`engines/homeostat_scan.py`, `engines/homeostat_render.py`, and
`pipeline.py`'s `s5 log-decision` command, so "find the latest S4 briefing"
and "read/write the decision log" exist in exactly one place rather than
three.
"""
from datetime import date
from pathlib import Path

import click
import yaml


def latest_s4_briefing(config: dict) -> tuple:
    """Returns (text, label) — label is the briefing's date-folder name, or
    None if System 4 has never produced one (an honest placeholder is
    returned as text in that case, not an exception — a stale or missing
    S4 briefing shouldn't stop the homeostat from running, see ADR 007
    Consequences).
    """
    briefing_root = Path(config.get("intelligence_dir", "intelligence")) / "s4" / "briefing"
    if briefing_root.is_dir():
        for run_dir in sorted((p for p in briefing_root.iterdir() if p.is_dir()), reverse=True):
            combined = run_dir / "combined.txt"
            if combined.exists():
                return combined.read_text(encoding="utf-8"), run_dir.name
    return "Todavía no se ha generado ningún briefing de System 4.", None


def load_decisions(root: Path) -> list:
    path = root / "decisions.yaml"
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or []
    except yaml.YAMLError:
        click.echo(f"  Warning: {path} is corrupted — treating decision history as empty.")
        return []


def record_decision(root: Path, tension: str, decision: str) -> None:
    path = root / "decisions.yaml"
    decisions = load_decisions(root)
    decisions.append({"date": date.today().isoformat(), "tension": tension, "decision": decision})
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(decisions, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
