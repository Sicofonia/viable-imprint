"""ADR 023, Decision 1 — the UI stays removable. Scans import statements; no Textual needed."""
import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import REPO_ROOT  # noqa: E402

SKIPPED_DIRS = {"__pycache__", "node_modules", "venv"}


def python_files(root: Path):
    for path in root.rglob("*.py"):
        parts = path.relative_to(REPO_ROOT).parts
        if any(p.startswith(".") or p in SKIPPED_DIRS for p in parts):
            continue
        yield path


def imported_modules(tree):
    """(module name, enclosing function name or None) for every import in a tree."""
    def walk(node, function):
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if isinstance(child, ast.Import):
                for alias in child.names:
                    yield alias.name, function
            elif isinstance(child, ast.ImportFrom) and child.module and child.level == 0:
                yield child.module, function
            yield from walk(child, inner)
    return list(walk(tree, None))


def is_tui(name):
    return name == "tui" or name.startswith("tui.")


class IsolationTests(unittest.TestCase):
    def test_nothing_outside_tui_imports_it_except_the_ui_command(self):
        offenders = []
        for path in python_files(REPO_ROOT):
            rel = path.relative_to(REPO_ROOT)
            # tests/live/smoke.py builds its commands with tui/services/commands.py (ADR 023, T8.2), so it
            # and its test go when the UI goes; see the removal procedure in the ADR.
            if (rel.parts[0] == "tui" or rel.parts[:2] in (("tests", "tui"), ("tests", "live"))
                    or rel.as_posix() == "tests/test_live_smoke.py"):
                continue
            for name, function in imported_modules(ast.parse(path.read_text(encoding="utf-8"))):
                if is_tui(name) and not (rel.as_posix() == "pipeline.py" and function == "ui"):
                    offenders.append(f"{rel}: import {name} (in {function or 'module level'})")
        self.assertEqual(offenders, [])

    def test_pipeline_does_import_tui_inside_the_ui_command(self):
        names = [(n, f) for n, f in imported_modules(ast.parse((REPO_ROOT / "pipeline.py").read_text(encoding="utf-8")))
                 if is_tui(n)]
        self.assertEqual(names, [("tui.app", "ui")])

    def test_services_and_the_package_init_import_no_textual(self):
        files = [REPO_ROOT / "tui" / "__init__.py", *(REPO_ROOT / "tui" / "services").rglob("*.py")]
        self.assertGreater(len(files), 1)
        for path in files:
            for name, _ in imported_modules(ast.parse(path.read_text(encoding="utf-8"))):
                self.assertNotIn(name.split(".")[0], ("textual", "rich"), f"{path.relative_to(REPO_ROOT)} imports {name}")


if __name__ == "__main__":
    unittest.main()
