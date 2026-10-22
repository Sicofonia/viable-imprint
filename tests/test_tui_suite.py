"""Loads the terminal UI's tests (ADR 023, Verification notes, item 6).

They live in tests/tui/ with no __init__.py: as a package it would be imported by
`unittest discover -s tests` as the top-level name `tui` and shadow the real
`tui/` package. Test modules there are named `test_tui_*.py` and are loaded here
with tests/tui/ as their own top-level directory, using a fresh loader so the
outer discovery's state is untouched. Modules that need Textual skip themselves
when it is not installed.
"""
import unittest
from pathlib import Path

TUI_TESTS = Path(__file__).resolve().parent / "tui"


def load_tests(loader, standard_tests, pattern):
    suite = unittest.TestSuite(standard_tests)
    inner = unittest.TestLoader()
    suite.addTests(inner.discover(str(TUI_TESTS), pattern="test_tui_*.py", top_level_dir=str(TUI_TESTS)))
    return suite
