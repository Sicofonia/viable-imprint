"""ADR 023, T1.4 — app shell: navigation, help, the run modal, one run at a time.
Needs Textual (skipped without it). Runs the real example-mode pipeline in
subprocesses, on fake providers.
"""
import asyncio
import logging
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    import textual  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Textual is not installed (uv sync --extra tui)")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402
from harness import EXAMPLE_LAUNCHER, GuardedCase, Workspace, ledger, make_book, root_group  # noqa: E402
from textual.widgets import Button, RichLog, Static  # noqa: E402
from tui.app import PipelineApp  # noqa: E402
from tui.screens import registry  # noqa: E402
from tui.screens.base import BaseScreen  # noqa: E402
from tui.screens.help import HelpScreen  # noqa: E402
from tui.screens.home import HomeScreen  # noqa: E402
from tui.screens.run import RunScreen, summarize  # noqa: E402
from tui.services import commands, estimate  # noqa: E402
from tui.services.command_model import build_model  # noqa: E402
from tui.services.context import Context  # noqa: E402
from tui.services.runner import RunResult  # noqa: E402
from tui.services.session import RunRequest  # noqa: E402
from tui.widgets.run_button import RunButton  # noqa: E402

SIZE = (120, 40)

logging.getLogger("asyncio").setLevel(logging.ERROR)  # debug-mode slow-callback noise
SLOW_SCRIPT = "import time\nprint('go', flush=True)\ntime.sleep(60)\n"


async def wait_for(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for a condition")
        await asyncio.sleep(0.05)


def log_text(screen) -> str:
    return "\n".join(strip.text for strip in screen.query_one("#run-log", RichLog).lines)


class ShellCase(GuardedCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)
        self.model = {s.name: s for s in build_model(root_group())}

    def app(self) -> PipelineApp:
        return PipelineApp(self.ctx, root_group())

    def argv(self, command, **values):
        return commands.argv_for(self.ctx, self.model[command], values)


class NavigationTests(ShellCase):
    async def test_every_screen_opens_by_key_and_escape_returns_home(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            self.assertIsInstance(app.screen, HomeScreen)
            for entry in registry.ENTRIES:
                await pilot.press(entry.key)
                await pilot.pause()
                self.assertEqual(type(app.screen).__name__, entry.cls, entry.name)
                await pilot.press("escape")
                await pilot.pause()
                self.assertIsInstance(app.screen, HomeScreen, entry.name)

    async def test_every_screen_opens_from_the_command_palette_commands(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            commands_by_title = {c.title: c for c in app.get_system_commands(app.screen)}
            for entry in registry.ENTRIES:
                commands_by_title[f"Open: {entry.title}"].callback()
                await pilot.pause()
                self.assertEqual(type(app.screen).__name__, entry.cls, entry.name)
            self.assertIn("Show help", commands_by_title)
            self.assertIn("Show the run log", commands_by_title)

    async def test_the_palette_itself_finds_a_screen_by_typing(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("ctrl+p")
            await pilot.pause()
            await pilot.press(*"explorer")
            await pilot.pause(1.0)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(type(app.screen).__name__, "ExplorerScreen")

    async def test_selecting_a_menu_entry_opens_it(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("enter")  # the menu is focused on the first entry
            await pilot.pause()
            self.assertEqual(type(app.screen).__name__, "ExplorerScreen")

    async def test_the_help_panel_lists_keys_and_the_one_run_at_a_time_rule(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("question_mark")
            await pilot.pause()
            self.assertIsInstance(app.screen, HelpScreen)
            keys = app.screen.query_one("#help-keys", Static).render().plain
            notes = app.screen.query_one("#help-notes", Static).render().plain
            self.assertIn("Command explorer", keys)
            self.assertIn("Quit", keys)
            self.assertIn("One command runs at a time", notes)
            self.assertIn("another terminal", notes)
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(app.screen, HomeScreen)


class RunModalTests(ShellCase):
    async def test_streams_a_run_and_shows_the_summary(self):
        make_book(self.ws, "tiny")
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            app.begin_run(RunRequest("s2 status tiny", self.argv("s2 status", book_slug="tiny")))
            await pilot.pause()
            self.assertIsInstance(app.screen, RunScreen)  # no estimate, so it started at once
            await app.session.wait()
            await pilot.pause()
            screen = app.screen
            self.assertEqual(screen.state, "done")
            self.assertIn("Book: tiny", log_text(screen))
            self.assertIn("cleanup", log_text(screen))
            self.assertIn("exit code 0", screen.query_one("#run-summary", Static).render().plain)
            self.assertIn("uv run python pipeline.py", screen.query_one("#run-hint").text)
            self.assertFalse(app.busy)
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(app.screen, HomeScreen)

    async def test_a_provider_call_asks_first_and_declining_runs_nothing(self):
        root = make_book(self.ws, "tiny")
        source = str(root / "s1b" / "source" / "tiny_source.txt")
        with harness.example_manifests():
            guess = estimate.estimate_command(self.ctx, ["s1b", "cleanup"], {"input_file": source})
        request = RunRequest("s1b cleanup", self.argv("s1b cleanup", input_file=source), estimate=guess)
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            app.begin_run(request)
            await pilot.pause()
            screen = app.screen
            self.assertEqual(screen.state, "confirm")
            text = screen.query_one("#run-confirm", Static).render().plain
            self.assertIn("This will call a provider", text)
            self.assertIn("Provider: fake / fake-1", text)
            self.assertIn("Provider calls: about 1", text)
            self.assertIn("uv run python pipeline.py", screen.query_one("#run-hint").text)  # the command is shown first

            await pilot.click("#run-close")  # "Cancel"
            await pilot.pause()
            self.assertIsInstance(app.screen, HomeScreen)
            self.assertIsNone(app.session)
            self.assertFalse(app.runner.busy)
            self.assertNotIn("s1b.cleanup", ledger(root))

            app.begin_run(request)
            await pilot.pause()
            await pilot.click("#run-go")
            await pilot.pause()
            await app.session.wait()
            await pilot.pause()
            self.assertEqual(ledger(root)["s1b.cleanup"]["status"], "done")
            self.assertIn("Saved:", log_text(app.screen))

    async def test_warnings_alone_force_a_confirmation(self):
        request = RunRequest("anything", [sys.executable, "-c", "print(1)"], warnings=["This overwrites an output."])
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            app.begin_run(request)
            await pilot.pause()
            self.assertEqual(app.screen.state, "confirm")
            self.assertIn("! This overwrites an output.", app.screen.query_one("#run-confirm", Static).render().plain)

    async def test_open_output_opens_the_last_saved_path(self):
        script = "print('Saved: C:/somewhere/first.txt')\nprint('Saved: C:/somewhere/last.txt')\n"
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            app.begin_run(RunRequest("saves", [sys.executable, "-c", script]))
            await pilot.pause()
            await app.session.wait()
            await pilot.pause()
            with mock.patch("tui.screens.run.open_path") as opened:
                await pilot.click("#run-open")
                await pilot.pause()
            opened.assert_called_once_with(Path("C:/somewhere/last.txt"))

    async def test_no_open_output_button_when_nothing_was_saved(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            app.begin_run(RunRequest("quiet", [sys.executable, "-c", "print('hello')"]))
            await pilot.pause()
            await app.session.wait()
            await pilot.pause()
            self.assertFalse(app.screen.query_one("#run-open").display)


class OneRunAtATimeTests(ShellCase):
    async def test_run_buttons_are_disabled_while_busy_and_the_log_can_be_reopened(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await app.push_screen(type("ProbeScreen", (BaseScreen,), {
                "compose_body": lambda self: iter([RunButton("Probe", id="probe")])})())
            await pilot.pause()
            button = app.screen.query_one("#probe", RunButton)
            self.assertFalse(button.disabled)

            app.begin_run(RunRequest("slow", [sys.executable, "-c", SLOW_SCRIPT]))
            await pilot.pause()
            await wait_for(lambda: "go" in app.session.lines)
            await pilot.pause()
            self.assertTrue(app.busy)
            self.assertTrue(button.disabled)
            self.assertEqual(app.sub_title, "Running: slow")

            first = app.session
            await pilot.press("escape")  # hide the log; the run keeps going
            await pilot.pause()
            self.assertIs(app.screen.query_one("#probe", RunButton), button)
            self.assertTrue(button.disabled)

            app.begin_run(RunRequest("second", [sys.executable, "-c", "print(1)"]))  # refused
            await pilot.pause()
            self.assertIs(app.session, first)
            self.assertEqual(first.request.title, "slow")

            await pilot.press("ctrl+r")  # bring the log back: it replays what it missed
            await pilot.pause()
            self.assertIsInstance(app.screen, RunScreen)
            self.assertIn("go", log_text(app.screen))
            self.assertEqual(app.screen.state, "running")

            await pilot.click("#run-cancel")
            await asyncio.wait_for(first.wait(), 30)
            await pilot.pause()
            self.assertEqual(app.screen.state, "done")
            self.assertIn("Cancelled", app.screen.query_one("#run-summary", Static).render().plain)
            self.assertFalse(app.busy)
            self.assertEqual(app.sub_title, "")
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.screen.query_one("#probe", RunButton).disabled)

    async def test_a_blocked_button_stays_disabled_with_its_reason(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await app.push_screen(type("ProbeScreen", (BaseScreen,), {
                "compose_body": lambda self: iter([RunButton("Probe", id="probe", blocked_reason="Nothing to run")])})())
            await pilot.pause()
            button = app.screen.query_one("#probe", RunButton)
            self.assertTrue(button.disabled)
            self.assertEqual(button.tooltip, "Nothing to run")
            button.blocked_reason = None
            self.assertFalse(button.disabled)

    async def test_screens_are_told_when_a_run_ends(self):
        seen = []
        probe = type("ProbeScreen", (BaseScreen,), {"run_finished": lambda self, result: seen.append(result.returncode)})
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await app.push_screen(probe())
            app.begin_run(RunRequest("quick", [sys.executable, "-c", "import sys; sys.exit(4)"]))
            await pilot.pause()
            await app.session.wait()
            await pilot.pause()
            self.assertEqual(seen, [4])


class SummaryTests(unittest.TestCase):
    def test_wording_by_outcome(self):
        base = dict(argv=[], seconds=2.0)
        self.assertIn("exit code 0", summarize(RunResult(returncode=0, **base)))
        self.assertIn("exit code 1", summarize(RunResult(returncode=1, **base)))
        self.assertIn("can be run again", summarize(RunResult(returncode=1, **base)))
        graceful = summarize(RunResult(returncode=1, cancelled=True, **base))
        self.assertIn("recorded in the ledger as a failed attempt", graceful)
        forced = summarize(RunResult(returncode=1, cancelled=True, forced=True, **base))
        self.assertIn("terminated", forced)
        self.assertIn("ledger was not updated", forced)
        self.assertIn("could not be started", summarize(RunResult(returncode=None, **base)))


if __name__ == "__main__":
    unittest.main()
