"""Shared data access for System 5's candidate evaluation calibration log
(ADR 013) — mirrors `lib/homeostat.py`'s `record_decision()` shape.
"""
from datetime import date
from pathlib import Path

import yaml


def record_calibration(root: Path, candidate_slug: str, agent_verdict: str, human_decision: str) -> None:
    """Append one entry to candidates/calibration.yaml — flat, append-only,
    never overwritten. See docs/adr/013-system-5-hardening.md.
    """
    path = root / "calibration.yaml"
    entries = []
    if path.exists():
        with open(path, encoding="utf-8") as f:
            entries = yaml.safe_load(f) or []
    entries.append({
        "candidate": candidate_slug,
        "agent_verdict": agent_verdict,
        "human_decision": human_decision,
        "date": date.today().isoformat(),
    })
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(entries, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
