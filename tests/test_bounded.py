"""The suite must not be able to hang: `harness.Bounded` fails a stuck async test and its
watchdog ends a run that is stuck in a way the timeout cannot see."""
import asyncio
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import REPO_ROOT, Bounded  # noqa: E402


class BoundedTests(unittest.TestCase):
    def test_an_async_test_that_never_ends_fails_instead_of_blocking(self):
        class Hanging(Bounded, unittest.IsolatedAsyncioTestCase):   # local: the loader must not collect it
            TEST_TIMEOUT = 0.3

            async def test_waits_forever(self):
                await asyncio.sleep(3600)

            async def test_finishes(self):
                await asyncio.sleep(0)

        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(Hanging).run(result)
        self.assertEqual(result.testsRun, 2)
        self.assertEqual(len(result.failures), 1)
        test, trace = result.failures[0]
        self.assertIn("test_waits_forever", str(test))
        self.assertIn("did not finish within 0.3 s", trace)

    def test_the_watchdog_ends_a_run_stuck_in_a_sync_test_and_names_it(self):
        script = textwrap.dedent(f"""
            import sys, time, unittest
            sys.path.insert(0, {str(REPO_ROOT / "tests")!r})
            from harness import Bounded

            class Stuck(Bounded, unittest.TestCase):
                WATCHDOG = 1.0
                def test_blocks_the_thread(self):
                    time.sleep(60)

            unittest.main(argv=["x"])
        """)
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        err = proc.stderr.decode("utf-8", "replace")
        self.assertIn("Timeout (0:00:01)", err)
        self.assertIn("test_blocks_the_thread", err)


if __name__ == "__main__":
    unittest.main()
