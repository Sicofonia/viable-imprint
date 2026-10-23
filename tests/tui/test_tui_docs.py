"""ADR 023, T8.3 — the README's "Using the UI" and "Tests" sections stay true: the keys match the
screens, and every documented command is valid. The smoke-test commands are only ever run as
dry runs here, so nothing reaches a real provider. No Textual needed.
"""
import re
import shlex
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "live"))

import smoke  # noqa: E402
from harness import GuardedCase, REPO_ROOT, Workspace, root_group  # noqa: E402
from tui.screens import registry  # noqa: E402

README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def section(start: str, end: str) -> str:
    return README[README.index(start):README.index(end)]


UI_SECTION = section("## Using the UI", "## Tests")
TEST_SECTION = section("## Tests", "## Licence")


def documented_commands(text: str) -> list:
    return [line.strip() for line in text.splitlines() if line.strip().startswith("uv ")]


class ReadmeKeysTests(unittest.TestCase):
    def test_every_screen_key_is_documented_with_its_title(self):
        keys = UI_SECTION[UI_SECTION.index("**Keys.**"):UI_SECTION.index("**What each screen does**")]
        for entry in registry.ENTRIES:
            self.assertIn(f"`{entry.key}`", keys, entry.name)
        self.assertIn("`n` new book", keys)
        for key in ("escape", "?", "ctrl+p", "ctrl+r", "q"):
            self.assertIn(f"`{key}`", keys)

    def test_every_screen_has_a_row_in_the_table(self):
        table = UI_SECTION[UI_SECTION.index("| Screen |"):UI_SECTION.index("**Rules the UI follows**")]
        for name in ("Home", "Book", "Command explorer", "Print prep", "Dashboard and sales", "Intelligence",
                     "Periodic", "Candidates", "Health"):
            self.assertIn(f"| {name}", table)

    def test_the_removal_procedure_names_what_the_adr_says_to_delete(self):
        for path in ("`tui/`", "`tests/tui/`", "`tests/live/`", "`tests/test_live_smoke.py`", "`ui` command", "`tui` extra"):
            self.assertIn(path, UI_SECTION)

    def test_the_cheat_sheet_points_at_the_explorer(self):
        top = (REPO_ROOT / "docs" / "cheat_sheet.txt").read_text(encoding="utf-8")[:700]
        self.assertIn("command explorer", top)
        self.assertIn("print prep screen", top)


class ReadmeCommandTests(GuardedCase):
    def test_the_install_and_launch_commands_are_the_documented_ones(self):
        commands = documented_commands(UI_SECTION)
        self.assertIn("uv sync --extra tui", commands[0])
        self.assertTrue(any(c.startswith("uv run python pipeline.py ui") for c in commands))

    def test_every_documented_smoke_command_parses_and_plans_when_made_a_dry_run(self):
        smoke_lines = [c for c in documented_commands(TEST_SECTION) if "tests/live/smoke.py" in c]
        self.assertEqual(len(smoke_lines), 4)
        with Workspace() as ws:
            for line in smoke_lines:
                args = shlex.split(line.split("#")[0])[shlex.split(line.split("#")[0]).index("tests/live/smoke.py") + 1:]
                if "--dry-run" not in args:
                    args.append("--dry-run")      # never reach a provider from a test
                printed = []
                code = smoke.main(args, load_env=False, root=root_group(), config_path=ws.config_path,
                                  environ={}, out=printed.append, confirm=lambda p: self.fail("asked"))
                self.assertEqual(code, 0, line)
                self.assertIn("Planned LLM calls", "\n".join(printed), line)

    def test_every_documented_unittest_target_exists(self):
        for line in documented_commands(TEST_SECTION):
            if "unittest" not in line:
                continue
            for word in shlex.split(line.split("#")[0]):
                if word.startswith("tests.tui."):
                    self.assertTrue((REPO_ROOT / (word.replace(".", "/") + ".py")).exists(), word)
        self.assertIn("-s tests", TEST_SECTION)


if __name__ == "__main__":
    unittest.main()
