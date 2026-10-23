"""ADR 023, T1.2 — command model (Click tree -> specs) and the argv builder.
No Textual needed.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402
from harness import EXAMPLE_LAUNCHER, GuardedCase, REPO_ROOT, Workspace, make_book, root_group, run_cli  # noqa: E402
from tui.services import commands  # noqa: E402
from tui.services.command_model import build_model, group_helps  # noqa: E402
from tui.services.context import Context  # noqa: E402


def click_leaf_paths(group, ctx=None):
    """The leaf command paths according to Click's own listing API."""
    ctx = ctx or click.Context(group)
    found = set()
    for name in group.list_commands(ctx):
        command = group.get_command(ctx, name)
        if isinstance(command, click.Group):
            found |= {(name, *rest) for rest in click_leaf_paths(command, click.Context(command, parent=ctx))}
        else:
            found.add((name,))
    return found


class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = root_group()
        cls.model = {spec.name: spec for spec in build_model(cls.root)}

    def test_command_paths_equal_what_click_reports(self):
        self.assertEqual({tuple(s.path) for s in self.model.values()}, click_leaf_paths(self.root))
        self.assertEqual(len(self.model), 42)  # 41 pipeline commands + `ui`

    def test_nested_groups_are_flattened_into_the_path(self):
        self.assertEqual(self.model["s2 homeostat run"].path, ["s2", "homeostat", "run"])
        self.assertIn("--step", self.model["s2 homeostat run"].param("step").opts)

    def test_engine_options_come_through(self):
        document = self.model["s1b translate"].param("document")
        self.assertEqual((document.kind, document.opts), ("flag", ["--document", "-d"]))
        template = self.model["s1b format"].param("template")
        self.assertEqual((template.kind, template.type, template.path_must_exist), ("option", "path", True))
        self.assertEqual(self.model["s1b cleanup"].param("input_file").kind, "argument")
        self.assertTrue(self.model["s1b cleanup"].param("input_file").required)
        self.assertEqual(self.model["s4 scan"].params, [])  # no input file: the root comes from config

    def test_set_cover_parameters_and_defaults(self):
        spec = self.model["s1c set-cover"]
        self.assertEqual([p.name for p in spec.params],
                         ["input_file", "output_file", "trim_w", "trim_h", "spine_w", "bleed", "unit"])
        by_name = {p.name: p for p in spec.params}
        self.assertEqual((by_name["trim_w"].type, by_name["trim_w"].required, by_name["trim_w"].default),
                         ("float", True, None))
        self.assertEqual((by_name["spine_w"].default, by_name["bleed"].default, by_name["unit"].default),
                         (0.0, 3.0, "mm"))
        self.assertEqual((by_name["unit"].type, by_name["unit"].choices), ("choice", ["mm", "pt"]))
        self.assertEqual(by_name["input_file"].opts, ["input_file"])

    def test_s2_run_only_has_the_book_systems_as_choices(self):
        only = self.model["s2 run"].param("only")
        self.assertEqual((only.kind, only.type, only.choices, only.default), ("option", "choice", ["s1b", "s1d"], None))
        self.assertEqual(self.model["s2 run"].param("step").kind, "flag")

    def test_help_text_and_group_labels(self):
        self.assertIn("Inspect a PDF's page boxes", self.model["s1c check"].help)
        self.assertIn("Run every currently-ready task", self.model["s2 run"].help)
        helps = group_helps(self.root)
        self.assertIn("System 4", helps[("s4",)])
        self.assertIn("homeostat", helps[("s2", "homeostat")].lower())

    def test_a_task_added_to_a_manifest_shows_up(self):
        from lib.task_loader import build_system_group
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            manifest_dir = Path(tmp) / "systems" / "sx"
            manifest_dir.mkdir(parents=True)
            (manifest_dir / "tasks.yaml").write_text(
                "tasks:\n  - name: extra\n    engine: translation\n    output: extra_out\n", encoding="utf-8")
            os.chdir(tmp)
            try:
                group = build_system_group("sx", "A test-only system")
            finally:
                os.chdir(previous)
        root = click.Group(name="root", commands={"sx": group})
        specs = {s.name: s for s in build_model(root)}
        self.assertEqual(list(specs), ["sx extra"])
        self.assertEqual([p.name for p in specs["sx extra"].params], ["input_file", "document"])  # engine CLI_OPTIONS too


class ArgvTests(GuardedCase):
    @classmethod
    def setUpClass(cls):
        cls.model = {s.name: s for s in build_model(root_group())}

    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path)
        self.base = commands.base_argv(self.ctx)

    def test_base_argv(self):
        self.assertEqual(self.base, [sys.executable, str(REPO_ROOT / "pipeline.py"), "--config", str(self.ws.config_path)])

    def test_a_simple_command_with_a_path_containing_spaces_and_non_ascii(self):
        path = str(self.ws.base / "mi libro ñandú.pdf")
        argv = commands.argv_for(self.ctx, self.model["s1c check"], {"input_file": path, "trim_w": "152", "trim_h": 229})
        self.assertEqual(argv, self.base + ["s1c", "check", path, "--trim-w", "152", "--trim-h", "229"])

    def test_defaults_flags_and_empty_values_are_left_out(self):
        values = {"input_file": "a.pdf", "output_file": "b.pdf", "trim_w": "304", "trim_h": "229",
                  "spine_w": "12", "bleed": "3.0", "unit": "mm"}
        argv = commands.argv_for(self.ctx, self.model["s1c set-cover"], values)
        self.assertEqual(argv[len(self.base):],
                         ["s1c", "set-cover", "a.pdf", "b.pdf", "--trim-w", "304", "--trim-h", "229", "--spine-w", "12"])
        values.update(bleed="5", unit="pt")
        self.assertEqual(commands.argv_for(self.ctx, self.model["s1c set-cover"], values)[-4:],
                         ["--bleed", "5", "--unit", "pt"])

    def test_flags_and_optional_paths(self):
        argv = commands.argv_for(self.ctx, self.model["s1b translate"], {"input_file": "c.txt", "document": True})
        self.assertEqual(argv[len(self.base):], ["s1b", "translate", "c.txt", "--document"])
        argv = commands.argv_for(self.ctx, self.model["s1b translate"], {"input_file": "c.txt", "document": False})
        self.assertEqual(argv[len(self.base):], ["s1b", "translate", "c.txt"])
        argv = commands.argv_for(self.ctx, self.model["s1b format"], {"input_file": "c.txt", "template": "t.odt"})
        self.assertEqual(argv[len(self.base):], ["s1b", "format", "c.txt", "--template", "t.odt"])

    def test_choice_option_and_nested_group(self):
        argv = commands.argv_for(self.ctx, self.model["s2 run"], {"book_slug": "tiny", "only": "s1b", "step": True})
        self.assertEqual(argv[len(self.base):], ["s2", "run", "tiny", "--only", "s1b", "--step"])
        argv = commands.argv_for(self.ctx, self.model["s2 homeostat run"], {})
        self.assertEqual(argv[len(self.base):], ["s2", "homeostat", "run"])

    def test_missing_required_values_raise_a_clear_error(self):
        with self.assertRaisesRegex(commands.MissingParameter, "--trim-w"):
            commands.argv_for(self.ctx, self.model["s1c set-interior"],
                              {"input_file": "a.pdf", "output_file": "b.pdf", "trim_h": "229"})
        with self.assertRaisesRegex(commands.MissingParameter, "INPUT_FILE"):
            commands.argv_for(self.ctx, self.model["s1b cleanup"], {})

    def test_invalid_values_are_rejected(self):
        values = {"input_file": "a.pdf", "trim_w": "wide", "trim_h": "229"}
        with self.assertRaises(commands.InvalidValue):
            commands.argv_for(self.ctx, self.model["s1c check"], values)
        with self.assertRaises(commands.InvalidValue):
            commands.argv_for(self.ctx, self.model["s2 run"], {"book_slug": "x", "only": "s9"})

    def test_a_positional_starting_with_a_dash_goes_after_a_separator(self):
        argv = commands.argv_for(self.ctx, self.model["s2 run"], {"book_slug": "-odd", "step": True})
        self.assertEqual(argv[len(self.base):], ["s2", "run", "--step", "--", "-odd"])

    def test_display_prefix_and_default_config_elision(self):
        argv = commands.argv_for(self.ctx, self.model["s2 status"], {"book_slug": "tiny"})
        shown = commands.display(argv)  # a non-default config is kept, in its place before the command
        self.assertTrue(shown.startswith("uv run python pipeline.py --config "), shown)
        self.assertIn(str(self.ws.config_path), shown)
        self.assertTrue(shown.endswith(" s2 status tiny"), shown)
        default = [sys.executable, str(REPO_ROOT / "pipeline.py"), "--config", str(REPO_ROOT / "config.yaml"),
                   "s2", "status", "tiny"]
        self.assertEqual(commands.display(default), "uv run python pipeline.py s2 status tiny")

    def test_display_quotes_what_needs_quoting(self):
        argv = [sys.executable, str(REPO_ROOT / "pipeline.py"), "--config", str(REPO_ROOT / "config.yaml"),
                "s1c", "check", "my book.pdf", "o'neil.pdf", "$x"]
        with mock.patch.object(commands.sys, "platform", "win32"):
            self.assertEqual(commands.display(argv),
                             "uv run python pipeline.py s1c check \"my book.pdf\" \"o'neil.pdf\" '$x'")
        with mock.patch.object(commands.sys, "platform", "linux"):
            self.assertEqual(commands.display(argv),
                             "uv run python pipeline.py s1c check 'my book.pdf' 'o'\"'\"'neil.pdf' '$x'")
        self.assertEqual(commands.display(argv[:6] + ["café/ñ.txt"]), "uv run python pipeline.py s1c check café/ñ.txt")

    def test_a_built_argv_gives_the_same_output_as_the_hand_typed_command(self):
        ctx = Context.load(self.ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)
        make_book(self.ws, "tiny")
        argv = commands.argv_for(ctx, self.model["s2 status"], {"book_slug": "tiny"})
        built = subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, timeout=120,
                               env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        code, typed = run_cli(self.ws, "s2", "status", "tiny", example=True)
        self.assertEqual((built.returncode, code), (0, 0))
        self.assertEqual((built.stdout + built.stderr).decode("utf-8"), typed)


class OnlyPlaceThatBuildsCommandLinesTests(unittest.TestCase):
    def test_the_config_flag_is_spelled_in_one_module_only(self):
        offenders = []
        for path in (REPO_ROOT / "tui").rglob("*.py"):
            if path.name != "commands.py" and '"--config"' in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
