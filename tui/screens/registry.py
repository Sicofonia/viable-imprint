"""The screens reachable from the home screen, their keys and where they live.

Classes are named by module path and imported on first use, so importing the
registry (for the home screen's key bindings and the command palette) loads no
screen code.
"""
import importlib
from dataclasses import dataclass


@dataclass(frozen=True)
class ScreenEntry:
    name: str
    key: str
    title: str
    description: str
    module: str
    cls: str

    def create(self):
        return getattr(importlib.import_module(self.module), self.cls)()


ENTRIES = (
    ScreenEntry("explorer", "e", "Command explorer", "every command, generated from the CLI",
                "tui.screens.explorer", "ExplorerScreen"),
    ScreenEntry("print_prep", "p", "Print prep (S1C)", "check and set page boxes on exported PDFs",
                "tui.screens.print_prep", "PrintPrepScreen"),
    ScreenEntry("performance", "d", "Dashboard and sales (S3)", "costs, revenue, sales ingest",
                "tui.screens.performance", "PerformanceScreen"),
    ScreenEntry("intelligence", "i", "Intelligence and articles (S4)", "scan, briefing, content strategy, article draft",
                "tui.screens.intelligence", "IntelligenceScreen"),
    ScreenEntry("periodic", "r", "Periodic pipelines", "homeostat and monthly newsletter",
                "tui.screens.periodic", "PeriodicScreen"),
    ScreenEntry("candidates", "c", "Candidates and policy (S5)", "evaluate candidates, calibration, policy sync",
                "tui.screens.candidates", "CandidatesScreen"),
    ScreenEntry("health", "h", "Health", "what would make a run fail",
                "tui.screens.health", "HealthScreen"),
)


def entry(name: str) -> ScreenEntry:
    return next(e for e in ENTRIES if e.name == name)
