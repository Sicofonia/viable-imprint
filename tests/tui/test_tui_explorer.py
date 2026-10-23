"""ADR 023, T1.5 — the command explorer. Needs Textual (skipped without it).
At this point every command in every system is usable from the UI.
"""
import asyncio
import logging
import sys
import time
import unittest
from pathlib import Path

try:
    import textual  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Textual is not installed (uv sync --extra tui)")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import EXAMPLE_LAUNCHER, GuardedCase, REPO_ROOT, Workspace, fixture, make_book, root_group, run_cli  # noqa: E402
from textual.widgets import Checkbox, Input, Select, Static  # noqa: E402
from tui.app import PipelineApp  # noqa: E402
from tui.screens.explorer import ExplorerScreen  # noqa: E402
from tui.screens.run import RunScreen  # noqa: E402
from tui.services.command_model import build_model  # noqa: E402
from tui.services.context import Context  # noqa: E402
from tui.widgets.param_form import NO_SELECTION, ParamForm, field_id  # noqa: E402

SIZE = (140, 45)

# asyncio debug mode (on in IsolatedAsyncioTestCase) logs every slow synchronous setup call.
logging.getLogger("asyncio").setLevel(logging.ERROR)


async def settle(pilot, times=3):
    for _ in range(times):
        await pilot.pause()


class ExplorerCase(GuardedCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)
        self.specs = build_model(root_group())

    def app(self):
        return PipelineApp(self.ctx, root_group())

    async def open_explorer(self, pilot):
        await pilot.press("e")
        await settle(pilot)
        explorer = pilot.app.screen
        self.assertIsInstance(explorer, ExplorerScreen)
        return explorer

    async def select(self, pilot, explorer, name):
        self.assertTrue(explorer.select_path(name.split()), name)
        await settle(pilot)
        self.assertEqual(explorer.spec.name, name)


class CoverageTests(ExplorerCase):
    async def test_every_command_can_be_selected_and_its_form_rendered(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            explorer = await self.open_explorer(pilot)
            self.assertEqual(len(explorer.visible_paths()), len(self.specs))
            for spec in self.specs:
                await self.select(pilot, explorer, spec.name)
                form = explorer.query_one(ParamForm)
                for param in spec.params:
                    self.assertEqual(len(form.query(f"#{field_id(param)}")), 1, f"{spec.name}: {param.name}")
                self.assertEqual(set(form.values()), {p.name for p in spec.params}, spec.name)
                self.assertTrue(explorer.query_one("#hint").text, spec.name)
                self.assertEqual(explorer.query_one("#detail-title", Static).render().plain, spec.name)

    async def test_field_types_follow_the_click_parameter_types(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            explorer = await self.open_explorer(pilot)
            await self.select(pilot, explorer, "s1c set-cover")
            form = explorer.query_one(ParamForm)
            unit = form.query_one("#param-unit", Select)
            self.assertEqual(unit.value, "mm")
            self.assertEqual(form.query_one("#param-trim_w", Input).type, "number")
            self.assertEqual(len(form.query("#browse-input_file")), 1)   # must exist: a picker
            self.assertEqual(len(form.query("#browse-output_file")), 0)  # a file to be created: just a name
            await self.select(pilot, explorer, "s1b translate")
            self.assertIsInstance(explorer.query_one("#param-document"), Checkbox)
            await self.select(pilot, explorer, "s2 run")
            only = explorer.query_one("#param-only", Select)
            self.assertEqual(only.value, NO_SELECTION)  # optional choice, no default

    async def test_the_command_line_hint_follows_the_form(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            explorer = await self.open_explorer(pilot)
            await self.select(pilot, explorer, "s1c set-cover")
            self.assertIn("Not ready yet", explorer.query_one("#hint").text)
            form = explorer.query_one(ParamForm)
            for name, value in (("input_file", "cover.pdf"), ("output_file", "cover_print.pdf"), ("trim_w", "304"),
                                ("trim_h", "229"), ("spine_w", "12")):
                form.set_value(name, value)
            await settle(pilot)
            hint = explorer.query_one("#hint").text
            self.assertIn("uv run python pipeline.py", hint)
            self.assertTrue(hint.rstrip().endswith(
                "s1c set-cover cover.pdf cover_print.pdf --trim-w 304 --trim-h 229 --spine-w 12"), hint)


class FilterTests(ExplorerCase):
    async def test_filtering_by_text_finds_the_right_commands(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            explorer = await self.open_explorer(pilot)
            box = explorer.query_one("#filter", Input)

            box.value = "cover"
            await settle(pilot)
            self.assertIn(["s1c", "set-cover"], explorer.visible_paths())
            self.assertNotIn(["s2", "run"], explorer.visible_paths())

            box.value = "isbn"
            await settle(pilot)
            self.assertIn(["book", "set-isbn"], explorer.visible_paths())
            self.assertNotIn(["s1c", "set-cover"], explorer.visible_paths())

            box.value = "zzzzz"
            await settle(pilot)
            self.assertEqual(explorer.visible_paths(), [])

            box.value = ""
            await settle(pilot)
            self.assertEqual(len(explorer.visible_paths()), len(self.specs))

    async def test_the_slash_key_focuses_the_filter(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            explorer = await self.open_explorer(pilot)
            await pilot.press("slash")
            await settle(pilot)
            self.assertIs(app.focused, explorer.query_one("#filter", Input))


class RunFromExplorerTests(ExplorerCase):
    async def test_status_from_the_explorer_matches_the_cli(self):
        make_book(self.ws, "tiny")
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            explorer = await self.open_explorer(pilot)
            await self.select(pilot, explorer, "s2 status")
            explorer.query_one(ParamForm).set_value("book_slug", "tiny")
            await settle(pilot)
            await pilot.click("#run")
            await settle(pilot)
            self.assertIsInstance(app.screen, RunScreen)
            await app.session.wait()
            await settle(pilot)
            code, typed = run_cli(self.ws, "s2", "status", "tiny", example=True)
            self.assertEqual(code, 0)
            self.assertEqual("\n".join(app.session.lines), typed.replace("\r\n", "\n").rstrip("\n"))

    async def test_a_missing_required_value_is_reported_and_nothing_runs(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            explorer = await self.open_explorer(pilot)
            await self.select(pilot, explorer, "s1b cleanup")
            await pilot.click("#run")
            await settle(pilot)
            self.assertIs(app.screen, explorer)
            self.assertIn("Cannot run yet: Missing required argument: INPUT_FILE", explorer.query_one("#hint").text)
            self.assertIsNone(app.session)

    async def test_a_provider_command_asks_before_running(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            explorer = await self.open_explorer(pilot)
            await self.select(pilot, explorer, "s1b cleanup")
            explorer.query_one(ParamForm).set_value("input_file", str(fixture("tiny_source.txt")))
            await settle(pilot)
            await pilot.click("#run")
            await settle(pilot)
            self.assertIsInstance(app.screen, RunScreen)
            self.assertEqual(app.screen.state, "confirm")
            text = app.screen.query_one("#run-confirm", Static).render().plain
            self.assertIn("Provider: fake / fake-1", text)
            self.assertIn("Provider calls: about 1", text)
            await pilot.click("#run-close")
            await settle(pilot)
            self.assertIsNone(app.session)


class WarnedCommandTests(ExplorerCase):
    async def test_chain_stages_and_the_ui_command_have_no_run_button(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            explorer = await self.open_explorer(pilot)
            expected = {
                "s5 homeostat-scan": "s2 homeostat run", "s5 homeostat": "s2 homeostat run",
                "s5 homeostat-render": "s2 homeostat run", "s1d newsletter-scan": "s2 newsletter run",
                "s1d newsletter": "s2 newsletter run", "s1d newsletter-track": "s2 newsletter run",
                "ui": "terminal UI itself",
            }
            for name, pointer in expected.items():
                await self.select(pilot, explorer, name)
                self.assertFalse(explorer.query_one("#run").display, name)
                self.assertIn(pointer, explorer.query_one("#detail-warning", Static).render().plain, name)
            await self.select(pilot, explorer, "s5 homeostat-scan")
            self.assertIn("intelligence/", explorer.query_one("#detail-warning", Static).render().plain)
            await self.select(pilot, explorer, "s2 homeostat run")  # the real entry point has a button
            self.assertTrue(explorer.query_one("#run").display)
            self.assertEqual(explorer.query_one("#detail-warning", Static).render().plain, "")

    async def test_updating_the_policy_hash_needs_a_second_confirmation(self):
        watched = REPO_ROOT / "prompts" / "s5" / "policy_evaluation_task.txt"
        before = watched.read_bytes()
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            explorer = await self.open_explorer(pilot)
            await self.select(pilot, explorer, "s5 check-policy-sync")
            explorer.query_one(ParamForm).set_value("do_update", True)
            await settle(pilot)
            await pilot.click("#run")
            await settle(pilot)
            self.assertIsInstance(app.screen, RunScreen)
            self.assertEqual(app.screen.state, "confirm")  # asked, though nothing is estimated
            self.assertIn("rewrites the stored sync hash", app.screen.query_one("#run-confirm", Static).render().plain)
            await pilot.click("#run-close")  # decline
            await settle(pilot)
            self.assertIsNone(app.session)
        self.assertEqual(watched.read_bytes(), before)

    async def test_the_plain_policy_check_runs_without_asking(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            explorer = await self.open_explorer(pilot)
            await self.select(pilot, explorer, "s5 check-policy-sync")
            await pilot.click("#run")
            await settle(pilot)
            await app.session.wait()
            await settle(pilot)
            self.assertEqual(app.session.lines[0].split(" (")[0], "In sync")


class BrowseTests(ExplorerCase):
    async def test_browse_picks_a_file_into_the_field(self):
        target = self.ws.base / "pick" / "manuscript.txt"
        target.parent.mkdir()
        target.write_text("text", encoding="utf-8")
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            explorer = await self.open_explorer(pilot)
            await self.select(pilot, explorer, "s1b cleanup")
            explorer.query_one(ParamForm).set_value("input_file", str(self.ws.base / "pick" / "typed-first.txt"))
            await settle(pilot)
            await pilot.click("#browse-input_file")
            await settle(pilot)
            from textual.widgets import DirectoryTree
            tree = app.screen.query_one(DirectoryTree)
            deadline = time.monotonic() + 15
            while not tree.root.children and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            self.assertEqual([str(n.data.path) for n in tree.root.children], [str(target)])
            tree.move_cursor(tree.root.children[0])
            await pilot.pause()
            await pilot.press("enter")
            await settle(pilot)
            self.assertIs(app.screen, explorer)
            self.assertEqual(explorer.query_one("#param-input_file", Input).value, str(target))


if __name__ == "__main__":
    unittest.main()
