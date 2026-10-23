"""ADR 023, Decision 6 — lib/interrupt.py and the pipeline.py hook that enables it."""
import os
import signal
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import REPO_ROOT, root_group  # noqa: E402
from lib import interrupt  # noqa: E402

CHILD = (
    "import sys, time\n"
    "from lib import interrupt\n"
    "print('installed', interrupt.install_graceful_break(), flush=True)\n"
    "try:\n"
    "    time.sleep(30)\n"
    "except KeyboardInterrupt:\n"
    "    print('interrupted', flush=True)\n"
    "    sys.exit(3)\n"
)


class InstallTests(unittest.TestCase):
    def test_does_nothing_off_windows(self):
        with mock.patch.object(interrupt.sys, "platform", "linux"):
            self.assertFalse(interrupt.install_graceful_break())

    @unittest.skipUnless(sys.platform == "win32", "Windows console control events")
    def test_ctrl_break_raises_keyboard_interrupt_even_inside_a_long_sleep(self):
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONUNBUFFERED": "1"}
        proc = subprocess.Popen([sys.executable, "-c", CHILD], cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            self.assertEqual(proc.stdout.readline().decode().strip(), "installed True")
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            except OSError as exc:
                self.skipTest(f"no console to send CTRL_BREAK_EVENT through: {exc}")
            self.assertEqual(proc.wait(timeout=10), 3)
            self.assertIn("interrupted", proc.stdout.read().decode())
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()


class MainHookTests(unittest.TestCase):
    def run_main(self, set_variable):
        root_group()  # imports pipeline with the repository root as working directory
        import pipeline
        env = {interrupt.ENV_VAR: "1"} if set_variable else {}
        with mock.patch.dict(os.environ, env):
            if not set_variable:
                os.environ.pop(interrupt.ENV_VAR, None)
            with mock.patch.object(pipeline.interrupt, "install_graceful_break") as install, \
                    mock.patch.object(pipeline, "cli") as cli:
                pipeline.main()
        return install, cli

    def test_installed_only_when_the_environment_variable_is_set(self):
        install, cli = self.run_main(True)
        install.assert_called_once()
        cli.assert_called_once()

    def test_not_installed_otherwise(self):
        install, cli = self.run_main(False)
        install.assert_not_called()
        cli.assert_called_once()


if __name__ == "__main__":
    unittest.main()
