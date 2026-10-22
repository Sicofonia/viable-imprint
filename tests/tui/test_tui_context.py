"""ADR 023, T1.1 — services/context.py: repository root, config, chdir guard."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import GuardedCase, REPO_ROOT, Workspace  # noqa: E402
from tui.services.context import ConfigError, Context, find_repo_root  # noqa: E402


class ContextTests(GuardedCase):
    def test_repo_root_is_the_folder_with_pipeline_py(self):
        self.assertEqual(find_repo_root(), REPO_ROOT)
        self.assertTrue((find_repo_root() / "pipeline.py").exists())

    def test_load_changes_into_the_repo_root_from_anywhere(self):
        with Workspace() as ws, tempfile.TemporaryDirectory() as elsewhere:
            os.chdir(elsewhere)
            ctx = Context.load(ws.config_path)
            self.assertEqual(Path(os.getcwd()).resolve(), REPO_ROOT)
            self.assertEqual(ctx.repo_root, REPO_ROOT)
            self.assertEqual(ctx.pipeline_script, REPO_ROOT / "pipeline.py")
            self.assertEqual(ctx.config["llm"]["provider"], "fake")

    def test_a_relative_config_path_is_resolved_against_the_launch_directory(self):
        with Workspace() as ws:
            os.chdir(ws.base)
            ctx = Context.load("config.yaml")
            self.assertEqual(ctx.config_path, ws.config_path)

    def test_data_dirs_are_absolute_and_follow_the_config(self):
        with Workspace() as ws:
            ctx = Context.load(ws.config_path)
            self.assertEqual(ctx.data_dir("books_dir"), ws.books_dir)
            self.assertEqual(ctx.data_dir("newsletter_dir"), ws.newsletter_dir)
            with self.assertRaises(KeyError):
                ctx.data_dir("nope_dir")

    def test_unset_data_dirs_default_under_the_repo_root(self):
        with Workspace() as ws:
            ctx = Context.load(ws.config_path)
            ctx.config.pop("books_dir")
            self.assertEqual(ctx.data_dir("books_dir"), REPO_ROOT / "books")

    def test_reload_config_sees_edits(self):
        with Workspace() as ws:
            ctx = Context.load(ws.config_path)
            ws.update("llm", model="fake-2")
            ctx.reload_config()
            self.assertEqual(ctx.config["llm"]["model"], "fake-2")

    def test_unreadable_and_unparsable_configs_raise_a_clear_error(self):
        with Workspace() as ws:
            with self.assertRaises(ConfigError):
                Context.load(ws.base / "missing.yaml")
            bad = ws.base / "bad.yaml"
            bad.write_text("llm: [unclosed", encoding="utf-8")
            with self.assertRaises(ConfigError):
                Context.load(bad)

    def test_default_config_detection(self):
        with Workspace() as ws:
            self.assertFalse(Context.load(ws.config_path).uses_default_config)


if __name__ == "__main__":
    unittest.main()
