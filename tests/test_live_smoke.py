"""ADR 023, T8.2 — tests/live/smoke.py, exercised on fake providers (it never touches a real one
here). Most cases run the whole script end to end through the example launcher; every subprocess
has a time limit, and the script itself is never collected by `unittest discover`.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "live"))

import harness  # noqa: E402
import smoke  # noqa: E402
from harness import EXAMPLE_LAUNCHER, GuardedCase, REPO_ROOT, Workspace, root_group  # noqa: E402

CAP = 150  # seconds: no command in these tests may run longer, whatever the script allows


def bounded_runner(calls=None):
    def run(argv, timeout):
        if calls is not None:
            calls.append(list(argv))
        return smoke.subprocess_runner(argv, min(timeout, CAP))
    return run


def temp_workspaces():
    return set(Path(tempfile.gettempdir()).glob("vi-live-*"))


class SmokeCase(GuardedCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.before = temp_workspaces()
        self.addCleanup(self.remove_new_workspaces)
        self.lines = []

    def remove_new_workspaces(self):
        import shutil
        for path in temp_workspaces() - self.before:
            shutil.rmtree(path, ignore_errors=True)

    def go(self, *args, **kwargs):
        kwargs.setdefault("runner", bounded_runner())
        kwargs.setdefault("config_path", self.ws.config_path)
        kwargs.setdefault("pipeline_script", EXAMPLE_LAUNCHER)
        kwargs.setdefault("environ", {})
        kwargs.setdefault("confirm", lambda prompt: self.fail("asked for confirmation"))
        return smoke.main(list(args), load_env=False, root=root_group(), out=self.lines.append, **kwargs)

    @property
    def printed(self) -> str:
        return "\n".join(self.lines)


class PlanTests(unittest.TestCase):
    def test_planned_call_counts_for_both_scopes(self):
        core = smoke.plan("core", False)
        self.assertEqual([s.name for s in core], ["s1b cleanup", "s1d brief", "s1d synopsis", "s5 evaluate"])
        self.assertEqual(smoke.planned_calls(core), (4, 0))

        full = smoke.plan("full", False)
        self.assertEqual(smoke.planned_calls(full), (16, 0))   # 10 in the book chain + 6 single-call steps
        self.assertEqual(smoke.planned_calls(smoke.plan("full", True)), (16, 1))

    def test_the_book_chain_count_comes_from_the_manifests(self):
        book_llm = sum(1 for system in ("s1b", "s1d") for t in harness._real_tasks(system)
                       if t["engine"] == "llm_text" and t.get("book_scoped", True))
        chain = smoke.plan("full", False)[0]
        self.assertEqual((chain.name, chain.llm_calls), (f"s2 run {smoke.BOOK}", book_llm))
        periodic = {s.name: s.llm_calls for s in smoke.plan("full", False)[-2:]}
        self.assertEqual(periodic, {"s2 homeostat run": 1, "s2 newsletter run": 1})

    def test_every_input_is_tiny(self):
        for scope in ("core", "full"):
            self.assertEqual(smoke.oversized(scope), [])
            for path in smoke.input_files(scope):
                self.assertLessEqual(len(path.read_text(encoding="utf-8")), smoke.MAX_INPUT_CHARS, path.name)

    def test_the_script_is_not_collected_by_discovery(self):
        names = [p.name for p in (REPO_ROOT / "tests" / "live").glob("*.py")]
        self.assertEqual(names, ["smoke.py"])
        self.assertFalse([n for n in names if n.startswith("test")])

    def test_the_workspace_config_keeps_the_real_settings_and_swaps_only_the_folders_and_the_translation_provider(self):
        real = {"llm": {"provider": "z-ai", "model": "glm"}, "translation": {"provider": "etranslation", "source_lang": "EN"},
                "books_dir": "books", "s3": {"deviation": {"cost_multiplier": 3}}}
        skipped = smoke.Workspace(real, with_translate=False)
        kept = smoke.Workspace(real, with_translate=True)
        try:
            self.assertEqual(skipped.config["translation"], {"provider": "fake", "source_lang": "EN"})
            self.assertEqual(kept.config["translation"]["provider"], "etranslation")
            for w in (skipped, kept):
                self.assertEqual(w.config["llm"], real["llm"])
                self.assertEqual(w.config["s3"], real["s3"])
                for key in smoke.DATA_DIRS:
                    self.assertTrue(Path(w.config[key]).is_absolute())
                    self.assertTrue(str(w.config[key]).startswith(str(w.base)))
                self.assertEqual(yaml.safe_load(w.config_path.read_text(encoding="utf-8"))["books_dir"], w.config["books_dir"])
        finally:
            import shutil
            for w in (skipped, kept):
                shutil.rmtree(w.base, ignore_errors=True)

    def test_judge_forgives_only_a_missing_format_template(self):
        chain = smoke.Step("s2 run", ["s2", "run"], lambda ws: {}, 1, chain=True)
        out = "Done: 4, Failed: 1\n  [x] s1b.cleanup\n  [!] s1b.format\n"
        with mock.patch.object(smoke, "format_missing_files", return_value=True):
            self.assertEqual(smoke.judge(chain, 1, out)[0], True)
            self.assertEqual(smoke.judge(chain, 1, "Done: 3, Failed: 2\n  [!] s1b.format\n  [!] s1d.brief\n")[0], False)
        with mock.patch.object(smoke, "format_missing_files", return_value=False):
            self.assertEqual(smoke.judge(chain, 1, out)[0], False)
        self.assertEqual(smoke.judge(chain, 0, "")[0], True)
        self.assertEqual(smoke.judge(chain, 124, "timed out after 5 s and was killed"), (False, "timed out after 5 s and was killed"))


class RefusalTests(SmokeCase):
    def test_an_oversized_input_refuses_before_anything_runs(self):
        calls = []
        code = self.go("--scope", "core", "--yes", size_limit=100, runner=bounded_runner(calls))
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])
        self.assertIn("Refusing to start", self.printed)
        self.assertIn("tiny_source.txt", self.printed)
        self.assertIn("limit is 100", self.printed)
        self.assertEqual(temp_workspaces() - self.before, set())

    def test_a_missing_credential_is_named_and_nothing_runs(self):
        cfg = self.ws.base / "real.yaml"
        cfg.write_text(yaml.safe_dump({"llm": {"provider": "mistral", "model": "m"}, "translation": {"provider": "deepl"}}), encoding="utf-8")
        calls = []
        code = self.go("--scope", "core", "--yes", config_path=cfg, runner=bounded_runner(calls))
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])
        self.assertIn("MISTRAL_API_KEY", self.printed)
        self.assertNotIn("DEEPL_API_KEY", self.printed)   # `core` never translates
        self.assertEqual(temp_workspaces() - self.before, set())

    def test_a_config_without_a_model_is_refused(self):
        cfg = self.ws.base / "real.yaml"
        cfg.write_text(yaml.safe_dump({"llm": {"provider": "mistral"}}), encoding="utf-8")
        self.assertEqual(self.go("--scope", "core", "--yes", config_path=cfg), 2)
        self.assertEqual(self.go("--scope", "core", "--yes", config_path=self.ws.base / "nope.yaml"), 2)


class ConfirmationTests(SmokeCase):
    def test_the_preview_lists_what_will_happen_and_declining_runs_nothing(self):
        calls = []
        code = self.go("--scope", "full", confirm=lambda prompt: "n", runner=bounded_runner(calls))
        self.assertEqual((code, calls), (4, []))
        text = self.printed
        self.assertIn("fake / fake-1", text)
        self.assertIn("Planned LLM calls: about 16", text)
        self.assertIn("fake (translation skipped; --with-translate uses fake)", text)
        self.assertIn("Cost ceiling: $0.10 (PIPELINE_LIVE_MAX_USD)", text)
        self.assertIn("your own books, candidates and intelligence are untouched", text)
        self.assertIn("Declined; nothing was run.", text)
        self.assertEqual(temp_workspaces() - self.before, set())

    def test_yes_skips_the_question_and_anything_but_y_declines(self):
        asked = []
        self.assertEqual(self.go("--scope", "core", confirm=lambda p: (asked.append(p), "")[1]), 4)
        self.assertEqual(len(asked), 1)
        self.assertIn("Run it? [y/N]", asked[0])

    def test_the_etranslation_wait_is_stated_when_translation_is_on(self):
        cfg = self.ws.base / "real.yaml"
        cfg.write_text(yaml.safe_dump({"llm": {"provider": "fake", "model": "m"},
                                       "translation": {"provider": "etranslation",
                                                       "etranslation": {"receiver_url": "https://r.example/api"}}}), encoding="utf-8")
        env = {n: "x" for n in ("ETRANSLATION_APP_NAME", "ETRANSLATION_PASSWORD", "ETRANSLATION_RECEIVER_SECRET")}
        self.go("--scope", "full", "--with-translate", config_path=cfg, environ=env, confirm=lambda p: "n")
        self.assertIn("Planned LLM calls: about 16 and 1 translation request(s)", self.printed)
        self.assertIn("minutes, up to 2 hours", self.printed)

    def test_a_quota_warning_when_the_run_would_use_a_quarter_of_a_known_free_tier(self):
        cfg = self.ws.base / "real.yaml"
        cfg.write_text(yaml.safe_dump({"llm": {"provider": "google-aistudio", "model": "m"},
                                       "translation": {"provider": "fake"}}), encoding="utf-8")
        env = {"GOOGLE_AISTUDIO_API_KEY": "x"}
        self.go("--scope", "full", config_path=cfg, environ=env, confirm=lambda p: "n")
        self.assertIn("WARNING: 16 planned calls is 80% of google-aistudio's known free-tier quota (20 requests per day)", self.printed)
        self.lines.clear()
        self.go("--scope", "core", config_path=cfg, environ=env, confirm=lambda p: "n")   # 4 of 20 = 20%: under the line
        self.assertNotIn("WARNING", self.printed)


class DryRunTests(SmokeCase):
    def test_a_dry_run_prints_the_plan_and_creates_and_calls_nothing(self):
        calls = []
        code = self.go("--scope", "core", "--dry-run", runner=bounded_runner(calls))
        self.assertEqual((code, calls), (0, []))
        self.assertIn("Planned LLM calls: about 4", self.printed)
        self.assertIn("s1b cleanup; s1d brief; s1d synopsis; s5 evaluate", self.printed)
        self.assertEqual(temp_workspaces() - self.before, set())


class EndToEndTests(SmokeCase):
    def passes(self):
        return [l for l in self.lines if l.strip().startswith("PASS")]

    def test_core_runs_four_commands_on_the_fakes_with_the_uis_argv(self):
        calls = []
        code = self.go("--scope", "core", "--yes", runner=bounded_runner(calls))
        self.assertEqual(code, 0, self.printed)
        self.assertEqual(len(self.passes()), 4)
        self.assertIn("Result: 4 of 4 commands passed", self.printed)
        self.assertIn("LLM calls: about 4", self.printed)
        # setup (init, candidate new) then the four steps, every one built by tui/services/commands.py
        tails = [c[4:] for c in calls]
        self.assertEqual([t[:2] for t in tails], [["init", "smoke-book"], ["candidate", "new"], ["s1b", "cleanup"],
                                                   ["s1d", "brief"], ["s1d", "synopsis"], ["s5", "evaluate"]])
        workspace_config = Path(calls[0][3])
        self.assertNotEqual(workspace_config, self.ws.config_path)
        for argv in calls:
            self.assertEqual(argv[:3], [sys.executable, str(EXAMPLE_LAUNCHER), "--config"])
            self.assertEqual(Path(argv[3]), workspace_config)   # always the temporary config, never the real one
        workspace = workspace_config.parent
        self.assertTrue((workspace / "books" / "smoke-book" / "s1d" / "synopsis" / "tiny_source.txt").is_file())
        self.assertTrue((workspace / "candidates" / "s5" / "evaluate" / "smoke-candidate.txt").is_file())
        self.assertTrue(any((workspace / "logs").iterdir()))
        self.assertIn(f"Workspace: {workspace}", self.printed)
        self.assertTrue(workspace.exists())   # kept for inspection

    def test_clean_deletes_the_workspace(self):
        self.assertEqual(self.go("--scope", "core", "--yes", "--clean"), 0, self.printed)
        self.assertIn("(deleted: --clean)", self.printed)
        self.assertEqual(temp_workspaces() - self.before, set())

    def test_full_runs_the_chain_and_the_s4_s5_and_periodic_steps(self):
        code = self.go("--scope", "full", "--yes")
        self.assertEqual(code, 0, self.printed)
        self.assertEqual(len(self.passes()), 7)
        self.assertIn("Result: 7 of 7 commands passed", self.printed)
        workspace = next(iter(temp_workspaces() - self.before))
        for rel in ("intelligence/s4/briefing", "intelligence/s4/content-strategy", "intelligence/s1d/article-draft",
                    "homeostat/s5/homeostat-render", "newsletter/s1d/newsletter-track", "candidates/s5/evaluate"):
            self.assertTrue(any((workspace / rel).rglob("*.*")), rel)
        ledger = yaml.safe_load((workspace / "books" / "smoke-book" / "manifest.yaml").read_text(encoding="utf-8"))["tasks"]
        self.assertEqual(ledger["s1b.translate"]["provider"], "fake")   # translation skipped by default

    def test_a_failing_command_is_reported_and_the_exit_code_says_so(self):
        self.ws.update("llm", fail=True)
        code = self.go("--scope", "core", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("FAIL", self.printed)
        self.assertIn("configured to fail", self.printed.replace("\r", ""))

    def test_a_command_that_outlives_its_time_limit_fails_instead_of_hanging(self):
        code, output = smoke.subprocess_runner([sys.executable, "-c", "import time; time.sleep(60)"], 1)
        self.assertEqual(code, 124)
        self.assertIn("timed out after 1 s and was killed", output)


class CeilingTests(SmokeCase):
    def test_the_run_stops_as_soon_as_the_recorded_cost_exceeds_the_ceiling(self):
        calls = []
        code = self.go("--scope", "core", "--yes", environ={"PIPELINE_LIVE_MAX_USD": "0.0000001"}, runner=bounded_runner(calls))
        self.assertEqual(code, 3, self.printed)
        self.assertEqual([c[4] for c in calls], ["init", "candidate", "s1b"])   # setup, then only the first step
        self.assertIn("exceeds the $0.00 ceiling", self.printed)
        self.assertIn("1 of 4 commands passed, 3 not run", self.printed)

    def test_the_default_ceiling_is_ten_cents(self):
        self.go("--scope", "core", "--dry-run")
        self.assertIn("$0.10", self.printed)
        self.assertEqual(smoke.DEFAULT_MAX_USD, 0.10)


if __name__ == "__main__":
    unittest.main()
