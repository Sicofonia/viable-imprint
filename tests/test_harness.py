"""ADR 023, T0.3 — the shared test harness itself, and the first end-to-end
runs through it: one CLI task and one in-process task, both on fake providers.
"""
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402
from harness import REPO_ROOT, Workspace, fixture, ledger, make_book, run_cli  # noqa: E402
from lib import task_loader  # noqa: E402


class GuardedCase(unittest.TestCase):
    """Fails the test if it touched the operator's real data folders."""

    def setUp(self):
        self.guard = harness.RealDataGuard()
        self.addCleanup(self.guard.assert_unchanged)


class WorkspaceTests(GuardedCase):
    def test_config_uses_absolute_temp_folders_and_the_fixed_pricing(self):
        with Workspace() as ws:
            for key in harness.DATA_DIR_KEYS:
                path = Path(ws.config[key])
                self.assertTrue(path.is_absolute(), key)
                self.assertTrue(path.is_dir(), key)
                self.assertTrue(str(path).startswith(str(ws.base)), key)
            self.assertEqual(ws.config["llm"]["provider"], "fake")
            self.assertEqual(ws.config["llm"]["pricing"], harness.PRICING_LLM)
            self.assertEqual(os.getcwd(), str(REPO_ROOT))
            base = ws.base
        self.assertFalse(base.exists())

    def test_sections_can_be_overridden_and_updated(self):
        with Workspace(llm={"fail": True}) as ws:
            self.assertTrue(ws.config["llm"]["fail"])
            ws.update("llm", fail=False)
            self.assertIn("fail: false", ws.config_path.read_text(encoding="utf-8"))

    def test_make_book_runs_init_and_copies_the_source(self):
        with Workspace() as ws:
            root = make_book(ws, "tiny-book")
            self.assertTrue((root / "manifest.yaml").exists())
            copied = root / "s1b" / "source" / "tiny_source.txt"
            self.assertEqual(copied.read_text(encoding="utf-8"), fixture("tiny_source.txt").read_text(encoding="utf-8"))

    def test_example_tasks_are_redirected_to_files_that_ship_with_the_repo(self):
        for system in ("s1b", "s1d", "s4", "s5"):
            for task in harness.example_tasks(system):
                for key in ("prompt", "reduce_prompt", "metadata_config", "sources_config"):
                    if task.get(key):
                        self.assertTrue((REPO_ROOT / task[key]).exists(), f"{system}.{task['name']}.{key}: {task[key]}")

    def test_require_personal_files_skips_naming_the_missing_file(self):
        missing = REPO_ROOT / "prompts" / "s1b" / "no_such_task.txt"
        with mock.patch.object(harness, "personal_files", return_value=[missing]):
            with self.assertRaises(unittest.SkipTest) as caught:
                harness.require_personal_files("s1b", "cleanup")
        self.assertIn("no_such_task.txt", str(caught.exception))

    def test_personal_files_for_format_include_the_template_named_in_the_style_config(self):
        needed = [p.name for p in harness.personal_files("s1b", "format")]
        self.assertEqual(needed[0], "format_styles.yaml")
        if (REPO_ROOT / "templates" / "format_styles.yaml").exists():
            self.assertTrue(needed[1].endswith(".odt"))


class EndToEndTests(GuardedCase):
    def assert_cleanup_recorded(self, root):
        entry = ledger(root)["s1b.cleanup"]
        self.assertEqual(entry["status"], "done")
        self.assertIsInstance(entry["duration_seconds"], float)
        self.assertEqual(entry["provider"], "fake")
        self.assertGreater(entry["usage"]["total_tokens"], 0)
        expected = round(entry["usage"]["prompt_tokens"] / 1e6 * 1.0 + entry["usage"]["completion_tokens"] / 1e6 * 2.0, 6)
        self.assertEqual(entry["cost_usd"], expected)

    def test_real_cli_cleanup_with_fakes(self):
        harness.require_personal_files("s1b", "cleanup")
        with Workspace() as ws:
            root = make_book(ws, "tiny-book")
            code, output = run_cli(ws, "s1b", "cleanup", root / "s1b" / "source" / "tiny_source.txt")
            self.assertEqual(code, 0, output)
            self.assert_cleanup_recorded(root)

    def test_example_launcher_cleanup_with_fakes(self):
        with Workspace() as ws:
            root = make_book(ws, "tiny-book")
            code, output = run_cli(ws, "s1b", "cleanup", root / "s1b" / "source" / "tiny_source.txt", example=True)
            self.assertEqual(code, 0, output)
            self.assert_cleanup_recorded(root)
            self.assertTrue((root / "s1b" / "cleaned" / "tiny_source.txt").exists())

    def test_run_task_in_process_with_the_example_prompt(self):
        with Workspace() as ws:
            root = make_book(ws, "tiny-book")
            source = root / "s1b" / "source" / "tiny_source.txt"
            with contextlib.redirect_stdout(io.StringIO()):
                output = task_loader.run_task(root, ws.config, "s1b", harness.example_task("s1b", "cleanup"),
                                              input_file=source)
            self.assertEqual(output, root / "s1b" / "cleaned" / "tiny_source.txt")
            self.assert_cleanup_recorded(root)


if __name__ == "__main__":
    unittest.main()
