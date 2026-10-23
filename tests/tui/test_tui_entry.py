"""ADR 023, T1.1 — the `ui` command and the optional dependency. These tests do not
need Textual: they simulate its absence.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import GuardedCase, REPO_ROOT, Workspace  # noqa: E402

# Runs pipeline.py as if `import textual` failed.
WITHOUT_TEXTUAL = ("import sys; sys.modules['textual'] = None; sys.argv = ['pipeline.py'] + sys.argv[1:]; "
                   "import pipeline; pipeline.main()")


def run_without_textual(config_path, *args):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, "-c", WITHOUT_TEXTUAL, "--config", str(config_path), *args],
                          cwd=REPO_ROOT, env=env, capture_output=True, timeout=120)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


class WithoutTextualTests(GuardedCase):
    def test_help_and_an_existing_command_still_work(self):
        with Workspace() as ws:
            code, out, _ = run_without_textual(ws.config_path, "--help")
            self.assertEqual(code, 0)
            self.assertIn("ui ", out)
            code, out, err = run_without_textual(ws.config_path, "s3", "dashboard")
            self.assertEqual(code, 0, err)
            self.assertIn("No books found", out)

    def test_ui_prints_the_install_hint_and_exits_non_zero(self):
        with Workspace() as ws:
            code, out, err = run_without_textual(ws.config_path, "ui")
            self.assertNotEqual(code, 0)
            self.assertIn("uv sync --extra tui", err)
            self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
