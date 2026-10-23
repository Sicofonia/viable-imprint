"""ADR 023, T1.3 — the subprocess runner. No Textual needed.

First against small `python -c` scripts, then against the real pipeline with a
fake provider that sleeps (the cancel path through `_run_recorded()`).
"""
import asyncio
import logging
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import EXAMPLE_LAUNCHER, Bounded, GuardedCase, REPO_ROOT, Workspace, ledger, make_book, root_group  # noqa: E402
from tui.services import commands  # noqa: E402
from tui.services.command_model import build_model  # noqa: E402
from tui.services.context import Context  # noqa: E402
from tui.services.runner import RunnerBusy, TaskRunner  # noqa: E402

# Mimics pipeline.main(): installs the graceful-break handler when the runner asks for it.
INTERRUPTIBLE = (
    "import os, sys, time\n"
    f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
    "from lib import interrupt\n"
    "if os.environ.get(interrupt.ENV_VAR):\n"
    "    interrupt.install_graceful_break()\n"
    "print('ready', flush=True)\n"
    "try:\n"
    "    time.sleep(60)\n"
    "except KeyboardInterrupt:\n"
    "    print('interrupted', flush=True)\n"
    "    sys.exit(3)\n"
)

STUBBORN = (
    "import signal, sys, time\n"
    "signal.signal(signal.SIGBREAK if sys.platform == 'win32' else signal.SIGINT, signal.SIG_IGN)\n"
    "print('ready', flush=True)\n"
    "time.sleep(60)\n"
)


# IsolatedAsyncioTestCase turns on asyncio debug mode, which logs every synchronous setup call
# (make_book runs a subprocess) as a slow callback. Not interesting here.
logging.getLogger("asyncio").setLevel(logging.ERROR)


def python(code):
    return [sys.executable, "-c", code]


class ScriptTests(Bounded, unittest.IsolatedAsyncioTestCase):
    async def test_lines_arrive_in_order_and_before_the_process_exits(self):
        arrivals = []
        code = "import time\nprint('one', flush=True)\ntime.sleep(1.0)\nprint('two', flush=True)\n"
        started = time.monotonic()
        result = await TaskRunner().run(python(code), lambda line: arrivals.append((line, time.monotonic() - started)))
        self.assertEqual([line for line, _ in arrivals], ["one", "two"])
        self.assertEqual(result.lines, ["one", "two"])
        self.assertEqual(result.returncode, 0)
        first, second = arrivals[0][1], arrivals[1][1]
        self.assertGreater(second - first, 0.6)  # "one" was delivered while the script was still sleeping
        self.assertGreaterEqual(result.seconds, 1.0)

    async def test_non_zero_exit_codes_are_returned(self):
        result = await TaskRunner().run(python("import sys\nprint('bye')\nsys.exit(7)\n"), lambda line: None)
        self.assertEqual((result.returncode, result.cancelled, result.forced), (7, False, False))

    async def test_stderr_is_merged_and_non_ascii_text_is_intact(self):
        code = "import sys\nprint('ñandú — café ✓')\nprint('error á', file=sys.stderr)\n"
        result = await TaskRunner().run(python(code), lambda line: None)
        self.assertEqual(result.lines, ["ñandú — café ✓", "error á"])

    async def test_a_very_long_line_is_not_a_problem(self):
        result = await TaskRunner().run(python("print('x' * 300000)"), lambda line: None)
        self.assertEqual(len(result.lines[0]), 300000)

    async def test_a_command_that_cannot_start_returns_no_exit_code(self):
        lines = []
        result = await TaskRunner().run(["no-such-program-here"], lines.append)
        self.assertIsNone(result.returncode)
        self.assertIn("Could not start", lines[0])

    async def test_the_working_directory_is_honoured(self):
        result = await TaskRunner(cwd=REPO_ROOT).run(python("import os\nprint(os.getcwd())"), lambda line: None)
        self.assertEqual(Path(result.lines[0]).resolve(), REPO_ROOT)

    async def test_cancel_interrupts_a_sleeping_script_gracefully(self):
        runner = TaskRunner()
        ready = asyncio.Event()
        task = asyncio.create_task(runner.run(python(INTERRUPTIBLE), lambda line: ready.set() if line == "ready" else None))
        await asyncio.wait_for(ready.wait(), 30)
        await runner.cancel(grace_seconds=10)
        result = await task
        self.assertTrue(result.cancelled)
        self.assertFalse(result.forced)
        self.assertEqual(result.returncode, 3)
        self.assertIn("interrupted", result.lines)
        self.assertFalse(runner.busy)

    async def test_a_script_that_ignores_the_interrupt_is_terminated(self):
        runner = TaskRunner()
        ready = asyncio.Event()
        task = asyncio.create_task(runner.run(python(STUBBORN), lambda line: ready.set() if line == "ready" else None))
        await asyncio.wait_for(ready.wait(), 30)
        started = time.monotonic()
        await runner.cancel(grace_seconds=1)
        result = await task
        self.assertTrue(result.cancelled)
        self.assertTrue(result.forced)
        self.assertLess(time.monotonic() - started, 15)
        self.assertNotEqual(result.returncode, 0)

    async def test_cancel_with_nothing_running_is_a_no_op(self):
        await TaskRunner().cancel(grace_seconds=0.1)

    async def test_a_second_run_while_busy_raises_and_leaves_the_first_alone(self):
        runner = TaskRunner()
        ready = asyncio.Event()
        task = asyncio.create_task(runner.run(python(INTERRUPTIBLE), lambda line: ready.set() if line == "ready" else None))
        await asyncio.wait_for(ready.wait(), 30)
        self.assertTrue(runner.busy)
        with self.assertRaises(RunnerBusy):
            await runner.run(python("print('second')"), lambda line: None)
        await runner.cancel(grace_seconds=10)
        first = await task
        self.assertTrue(first.cancelled)
        self.assertFalse(runner.busy)
        again = await runner.run(python("print('now free')"), lambda line: None)  # usable again
        self.assertEqual(again.lines, ["now free"])

    async def test_cancelling_the_awaiting_task_does_not_leave_a_process_behind(self):
        runner = TaskRunner()
        ready = asyncio.Event()
        task = asyncio.create_task(runner.run(python(INTERRUPTIBLE), lambda line: ready.set() if line == "ready" else None))
        await asyncio.wait_for(ready.wait(), 30)
        proc = runner._proc
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNotNone(proc.returncode)
        self.assertFalse(runner.busy)


class PipelineIntegrationTests(GuardedCase, unittest.IsolatedAsyncioTestCase):
    def build(self, ws, slug_args):
        ctx = Context.load(ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)
        spec = next(s for s in build_model(root_group()) if s.path == slug_args[0])
        return commands.argv_for(ctx, spec, slug_args[1]), ctx

    async def test_cleanup_through_the_runner_records_done(self):
        with Workspace() as ws:
            root = make_book(ws, "tiny")
            argv, ctx = self.build(ws, (["s1b", "cleanup"], {"input_file": str(root / "s1b" / "source" / "tiny_source.txt")}))
            lines = []
            result = await TaskRunner(cwd=ctx.repo_root).run(argv, lines.append)
            self.assertEqual(result.returncode, 0, "\n".join(result.lines))
            self.assertTrue(any(line.startswith("Saved:") for line in lines))
            self.assertEqual(ledger(root)["s1b.cleanup"]["status"], "done")

    async def test_cancelling_a_slow_task_records_failed_in_the_ledger(self):
        with Workspace(llm={"delay_seconds": 5}) as ws:
            root = make_book(ws, "tiny")
            argv, ctx = self.build(ws, (["s1b", "cleanup"], {"input_file": str(root / "s1b" / "source" / "tiny_source.txt")}))
            runner = TaskRunner(cwd=ctx.repo_root)
            at_provider_call = asyncio.Event()
            task = asyncio.create_task(runner.run(
                argv, lambda line: at_provider_call.set() if "chunk 1/1" in line else None))
            await asyncio.wait_for(at_provider_call.wait(), 60)
            await asyncio.sleep(0.5)  # inside the fake provider's 5-second sleep
            started = time.monotonic()
            await runner.cancel(grace_seconds=10)
            result = await task
            self.assertLess(time.monotonic() - started, 4)  # interrupted, not waited out
            self.assertTrue(result.cancelled)
            self.assertFalse(result.forced, "\n".join(result.lines))
            self.assertNotEqual(result.returncode, 0)
            entry = ledger(root)["s1b.cleanup"]
            self.assertEqual(entry["status"], "failed")
            self.assertEqual(entry["error"], "KeyboardInterrupt")


if __name__ == "__main__":
    unittest.main()
