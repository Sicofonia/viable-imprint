"""ADR 023, T1.1 — the app starts and quits. Needs Textual (skipped without it)."""
import sys
import unittest
from pathlib import Path

try:
    import textual  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Textual is not installed (uv sync --extra tui)")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import GuardedCase, Workspace, root_group  # noqa: E402
from tui.app import PipelineApp  # noqa: E402
from tui.screens.home import HomeScreen  # noqa: E402
from tui.services.context import Context  # noqa: E402


class AppStartTests(GuardedCase, unittest.IsolatedAsyncioTestCase):
    async def test_starts_on_the_home_screen_and_quits_with_q(self):
        with Workspace() as ws:
            app = PipelineApp(Context.load(ws.config_path), root_group())
            async with app.run_test() as pilot:
                self.assertIsInstance(app.screen, HomeScreen)
                await pilot.press("q")
            self.assertFalse(app.is_running)


if __name__ == "__main__":
    unittest.main()
