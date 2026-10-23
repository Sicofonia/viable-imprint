"""ADR 023, T7.2 — policy sync. Always against temporary copies of docs/vsm.md and the S5
prompt (the path parameters of `policy_check`), never the real files. The status helper needs
no Textual; the screen tests skip without it.
"""
import logging
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402
from harness import EXAMPLE_LAUNCHER, GuardedCase, REPO_ROOT, Workspace, root_group  # noqa: E402
from tui.services import state  # noqa: E402
from tui.services.context import Context  # noqa: E402

logging.getLogger("asyncio").setLevel(logging.ERROR)

REAL_FILES = (REPO_ROOT / "docs" / "vsm.md", REPO_ROOT / "prompts" / "s5" / "policy_evaluation_task.txt")
HEADING = "### Editorial Policy (Constitutive Criteria)"


class PolicyCase(GuardedCase):
    def copies(self, ws):
        """Temporary copies of the two files; the real ones are asserted untouched at the end."""
        before = [p.read_bytes() for p in REAL_FILES]
        self.addCleanup(lambda: self.assertEqual(before, [p.read_bytes() for p in REAL_FILES]))
        vsm, prompt = ws.base / "vsm.md", ws.base / "prompt.txt"
        shutil.copy(REAL_FILES[0], vsm)
        shutil.copy(REAL_FILES[1], prompt)
        return vsm, prompt

    def drift(self, vsm):
        text = vsm.read_text(encoding="utf-8")
        assert HEADING in text
        vsm.write_text(text.replace(HEADING, HEADING + "\n\nA criterion added after the prompt was synced.", 1), encoding="utf-8")


class PolicyStatusTests(PolicyCase):
    def test_in_sync(self):
        with Workspace() as ws:
            vsm, prompt = self.copies(ws)
            status = state.policy_status(vsm, prompt)
            self.assertTrue(status.in_sync)
            self.assertEqual(status.stored, status.current)
            self.assertIn("in sync", status.sentence())
            self.assertIn(status.current, status.sentence())

    def test_out_of_sync_names_both_hashes(self):
        with Workspace() as ws:
            vsm, prompt = self.copies(ws)
            in_sync = state.policy_status(vsm, prompt)
            self.drift(vsm)
            status = state.policy_status(vsm, prompt)
            self.assertFalse(status.in_sync)
            self.assertEqual(status.stored, in_sync.stored)
            self.assertNotEqual(status.current, status.stored)
            self.assertIn("OUT OF SYNC", status.sentence())
            self.assertIn(status.stored, status.sentence())
            self.assertIn(status.current, status.sentence())

    def test_a_prompt_with_no_stored_hash_is_out_of_sync(self):
        with Workspace() as ws:
            vsm, prompt = self.copies(ws)
            prompt.write_text("\n".join(l for l in prompt.read_text(encoding="utf-8").splitlines()
                                        if not l.startswith("# vsm-sync-hash")), encoding="utf-8")
            status = state.policy_status(vsm, prompt)
            self.assertEqual((status.in_sync, status.stored), (False, None))
            self.assertIn("(none recorded)", status.sentence())

    def test_a_restructured_policy_document_or_a_missing_file_is_reported_not_raised(self):
        with Workspace() as ws:
            vsm, prompt = self.copies(ws)
            vsm.write_text("# Nothing here\n", encoding="utf-8")
            status = state.policy_status(vsm, prompt)
            self.assertFalse(status.in_sync)
            self.assertIn("Could not find", status.error)
            self.assertIn("Cannot check", status.sentence())
            self.assertIn("Cannot check", state.policy_status(ws.base / "missing.md", prompt).sentence())

    def test_the_status_agrees_with_the_cli_check_on_the_real_files(self):
        with Workspace() as ws:
            code, out = harness.run_cli(ws, "s5", "check-policy-sync", example=True)
            status = state.policy_status(*REAL_FILES)
            self.assertEqual(code == 0, status.in_sync, out)
            self.assertIn(status.current, out)


try:
    import textual  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Textual is not installed (uv sync --extra tui)")

from textual.widgets import Button, Static  # noqa: E402
from tui.app import PipelineApp  # noqa: E402
from tui.screens.candidates import CandidatesScreen  # noqa: E402
from tui.widgets.run_button import RunButton  # noqa: E402

SIZE = (170, 100)


async def settle(pilot, times=3):
    for _ in range(times):
        await pilot.pause()


class PolicyScreenTests(PolicyCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)

    async def open(self, pilot, app, files):
        await pilot.press("c")
        await settle(pilot)
        screen = app.screen
        self.assertIsInstance(screen, CandidatesScreen)
        screen.policy_files = files
        screen.refresh_state()
        await settle(pilot)
        return screen

    def banner(self, screen):
        return screen.query_one("#cand-policy", Static).render().plain

    async def test_in_sync_shows_the_hash_and_no_fix_buttons(self):
        files = self.copies(self.ws)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app, files)
            self.assertIn("in sync", self.banner(screen))
            self.assertFalse(screen.query_one("#cand-policy").has_class("problem"))
            self.assertFalse(screen.query_one("#cand-policy-actions").display)

    async def test_out_of_sync_warns_with_both_hashes_and_offers_the_fixes(self):
        vsm, prompt = self.copies(self.ws)
        self.drift(vsm)
        status = state.policy_status(vsm, prompt)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app, (vsm, prompt))
            text = self.banner(screen)
            self.assertIn("OUT OF SYNC", text)
            self.assertIn(status.stored, text)
            self.assertIn(status.current, text)
            self.assertTrue(screen.query_one("#cand-policy").has_class("problem"))
            self.assertTrue(screen.query_one("#cand-policy-actions").display)
            with mock.patch("tui.screens.candidates.open_path") as opened:
                await pilot.click("#policy-open-prompt")
                await pilot.click("#policy-open-vsm")
                await settle(pilot)
            self.assertEqual([c.args[0] for c in opened.call_args_list], [prompt, vsm])

    async def test_mark_as_synced_asks_for_confirmation_and_builds_the_update_command(self):
        vsm, prompt = self.copies(self.ws)
        self.drift(vsm)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app, (vsm, prompt))
            with mock.patch.object(app, "begin_run") as begin:   # never actually run: --update would touch the real prompt
                await pilot.click("#policy-sync")
                await settle(pilot)
            request = begin.call_args.args[0]
            self.assertEqual(request.argv[-3:], ["s5", "check-policy-sync", "--update"])
            self.assertTrue(request.confirm)
            self.assertTrue(request.needs_confirmation)
            self.assertIn("rewrites the stored sync hash", request.warnings[0])
            self.assertIn("reviewing that prompt against", request.warnings[0])

    async def test_declining_the_confirmation_runs_nothing(self):
        vsm, prompt = self.copies(self.ws)
        self.drift(vsm)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app, (vsm, prompt))
            await pilot.click("#policy-sync")
            await settle(pilot)
            self.assertEqual(app.screen.state, "confirm")
            self.assertIn("rewrites the stored sync hash", app.screen.query_one("#run-confirm", Static).render().plain)
            await pilot.click("#run-close")   # Cancel
            await settle(pilot)
            self.assertIsNone(app.session)

    async def test_an_unreadable_policy_document_disables_the_fix(self):
        vsm, prompt = self.copies(self.ws)
        vsm.write_text("# Nothing here\n", encoding="utf-8")
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app, (vsm, prompt))
            self.assertIn("Cannot check the policy prompt", self.banner(screen))
            sync = screen.query_one("#policy-sync", RunButton)
            self.assertTrue(sync.disabled)
            self.assertEqual(sync.tooltip, "Fix the problem above first")


if __name__ == "__main__":
    unittest.main()
