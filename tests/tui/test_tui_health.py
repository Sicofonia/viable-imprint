"""ADR 023, T8.1 — the health report. The checks need no Textual; the screen tests skip
without it. The fixtures are a throwaway repository tree with each item broken in turn;
the one check against the real repository is skipped where the personal files are missing.
"""
import logging
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402
from harness import EXAMPLE_LAUNCHER, GuardedCase, REPO_ROOT, Workspace, root_group  # noqa: E402
from tui.services import health  # noqa: E402
from tui.services.context import Context  # noqa: E402

logging.getLogger("asyncio").setLevel(logging.ERROR)

SECRET = "sekrit-value-9f3a1c"
GOOD_ENV = {"MISTRAL_API_KEY": SECRET, "DEEPL_API_KEY": SECRET}
VSM_OK = "# Doc\n\n### Editorial Policy (Constitutive Criteria)\nIn scope: travel.\n\n## Next\n"


def config(**overrides):
    base = {
        "llm": {"provider": "mistral", "model": "mistral-medium-latest",
                "pricing": {"prompt_per_million": 1.0, "completion_per_million": 2.0}},
        "translation": {"provider": "deepl", "source_lang": "EN", "target_lang": "ES",
                        "deepl": {"pricing": {"per_million_characters": 20.0}}},
    }
    for key, value in overrides.items():
        base[key] = value
    return base


class Fixture:
    """A repository tree with one system whose task names all five kinds of file."""

    def __init__(self, cfg=None):
        self.tmp = tempfile.TemporaryDirectory(prefix="vi-health-")
        self.repo = Path(self.tmp.name).resolve()
        (self.repo / "systems" / "sx").mkdir(parents=True)
        (self.repo / "systems" / "sx" / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
            {"name": "one", "engine": "llm_text", "prompt": "prompts/sx/one_task.txt", "reduce_prompt": "prompts/sx/one_reduce.txt",
             "metadata_config": "templates/meta.yaml"},
            {"name": "two", "engine": "odt_format", "style_config": "templates/format_styles.yaml"},
            {"name": "three", "engine": "feed_scan", "sources_config": "systems/sx/sources.yaml"},
        ]}), encoding="utf-8")
        for rel in ("prompts/sx/one_task.txt", "prompts/sx/one_reduce.txt", "templates/meta.yaml", "systems/sx/sources.yaml",
                    "templates/book.odt"):
            self.write(rel, "x")
        self.write("templates/format_styles.yaml", "template: templates/book.odt\nstyles: {}\n")
        self.write("docs/vsm.md", VSM_OK)
        for key in ("books", "intelligence", "candidates", "homeostat", "newsletter"):
            (self.repo / key).mkdir()
        self.cfg_path = self.repo / "config.yaml"
        self.cfg = cfg or config()
        self.cfg.update({f"{k}_dir": str(self.repo / k) for k in ("books", "intelligence", "candidates", "homeostat", "newsletter")})
        self.cfg_path.write_text(yaml.safe_dump(self.cfg), encoding="utf-8")
        self.ctx = Context(repo_root=self.repo, config_path=self.cfg_path, config=self.cfg)
        self.sync_policy()

    def write(self, rel, text):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def sync_policy(self):
        from lib import policy_check
        digest = policy_check.current_hash(str(self.repo / "docs" / "vsm.md"))
        self.write("prompts/s5/policy_evaluation_task.txt", f"# vsm-sync-hash: {digest}\nPrompt.\n")

    def run(self, **kwargs):
        kwargs.setdefault("environ", GOOD_ENV)
        kwargs.setdefault("which", lambda name: str(self.repo / "soffice"))
        kwargs.setdefault("policy_files", (self.repo / "docs" / "vsm.md", self.repo / "prompts" / "s5" / "policy_evaluation_task.txt"))
        return health.run_checks(self.ctx, **kwargs)

    def close(self):
        self.tmp.cleanup()


def find(checks, area=None, label=None, status=None):
    return [c for c in checks if (area is None or c.area == area) and (label is None or label in c.label)
            and (status is None or c.status == status)]


class HealthChecksTests(unittest.TestCase):
    def fixture(self, cfg=None):
        f = Fixture(cfg)
        self.addCleanup(f.close)
        return f

    def test_a_healthy_environment_has_no_errors_or_warnings(self):
        f = self.fixture()
        checks = f.run()
        self.assertEqual([c for c in checks if c.status in (health.ERROR, health.WARNING)], [])
        self.assertEqual(health.summary(checks), "Nothing would stop a run.")
        self.assertTrue(find(checks, "Config", status=health.OK))

    def test_an_unreadable_or_unparsable_config_stops_the_report_with_one_error(self):
        f = self.fixture()
        f.cfg_path.write_text("llm: [unclosed", encoding="utf-8")
        checks = f.run()
        self.assertEqual([(c.area, c.status) for c in checks], [("Config", health.ERROR)])
        self.assertIn("Cannot parse", checks[0].detail)
        f.cfg_path.unlink()
        self.assertIn("Cannot read", f.run()[0].detail)

    def test_llm_provider_and_model(self):
        f = self.fixture(config(llm={"model": "m"}))
        self.assertIn("llm.provider is not set", find(f.run(), "LLM provider")[0].detail)
        f = self.fixture(config(llm={"provider": "gpt-9", "model": "m"}))
        self.assertIn("Unknown provider 'gpt-9'", find(f.run(), "LLM provider")[0].detail)
        f = self.fixture(config(llm={"provider": "mistral"}))
        model = find(f.run(), "LLM provider", "Model")[0]
        self.assertEqual(model.status, health.ERROR)
        self.assertIn("llm.model is not set", model.detail)

    def test_a_missing_key_is_an_error_naming_the_variable_and_a_present_one_is_set(self):
        f = self.fixture()
        missing = find(f.run(environ={}), "LLM provider", "MISTRAL_API_KEY")[0]
        self.assertEqual((missing.status, missing.detail), (health.ERROR, "missing (add it to .env)"))
        present = find(f.run(environ={"MISTRAL_API_KEY": SECRET}), "LLM provider", "MISTRAL_API_KEY")[0]
        self.assertEqual((present.status, present.detail), (health.OK, "set"))
        self.assertEqual(find(f.run(environ={}), "Translation provider", "DEEPL_API_KEY")[0].status, health.ERROR)
        f2 = self.fixture(config(translation={"provider": "etranslation", "etranslation": {"receiver_url": "https://r.example/api"}}))
        names = {c.label for c in find(f2.run(environ={}), "Translation provider", status=health.ERROR)}
        self.assertEqual(names, {"ETRANSLATION_APP_NAME", "ETRANSLATION_PASSWORD", "ETRANSLATION_RECEIVER_SECRET"})

    def test_a_key_value_never_appears_in_any_text(self):
        f = self.fixture()
        for environ in (GOOD_ENV, {"MISTRAL_API_KEY": SECRET}):
            for check in f.run(environ=environ):
                for text in (check.area, check.label, check.detail, check.mark):
                    self.assertNotIn(SECRET, text)

    def test_the_etranslation_receiver_url_must_not_be_the_example_placeholder(self):
        env = {n: "x" for n in health.TRANSLATION_ENV["etranslation"]} | GOOD_ENV
        for url, expected in (("https://your-project.vercel.app/api/etranslation", "still the example placeholder"),
                              ("", "not set"), ("http://insecure.example/api", "https URL"),
                              ("https://r.example/api?x=1", "no query string")):
            f = self.fixture(config(translation={"provider": "etranslation", "etranslation": {"receiver_url": url}}))
            receiver = find(f.run(environ=env), "Translation provider", "receiver_url")[0]
            self.assertEqual(receiver.status, health.ERROR, url)
            self.assertIn(expected, receiver.detail, url)
        f = self.fixture(config(translation={"provider": "etranslation", "etranslation": {"receiver_url": "https://r.example/api"}}))
        self.assertEqual(find(f.run(environ=env), "Translation provider", "receiver_url")[0].status, health.OK)

    def test_pricing_configured_or_not_for_each_provider(self):
        f = self.fixture()
        self.assertEqual([c.status for c in find(f.run(), "Pricing")], [health.OK, health.OK])
        f = self.fixture(config(llm={"provider": "mistral", "model": "m"}, translation={"provider": "deepl"}))
        pricing = find(f.run(), "Pricing")
        self.assertEqual([c.status for c in pricing], [health.INFO, health.INFO])
        self.assertIn("no pricing configured", pricing[0].detail)

    def test_every_file_a_task_names_is_checked(self):
        f = self.fixture()
        for rel, key in (("prompts/sx/one_task.txt", "prompt"), ("prompts/sx/one_reduce.txt", "reduce_prompt"),
                         ("templates/meta.yaml", "metadata_config"), ("systems/sx/sources.yaml", "sources_config")):
            (f.repo / rel).unlink()
            errors = find(f.run(), "Task files", status=health.ERROR)
            self.assertEqual(len(errors), 1, rel)
            self.assertIn(key, errors[0].label)
            self.assertIn(rel, errors[0].detail)
            f.write(rel, "x")
        (f.repo / "templates" / "format_styles.yaml").unlink()
        self.assertIn("style_config", find(f.run(), "Task files", status=health.ERROR)[0].label)

    def test_a_missing_personal_file_points_at_the_example_to_copy(self):
        f = self.fixture()
        f.write("prompts/examples/sx/one_task.txt", "example")
        (f.repo / "prompts" / "sx" / "one_task.txt").unlink()
        error = find(f.run(), "Task files", status=health.ERROR)[0]
        self.assertIn("Copy prompts/examples/sx/one_task.txt to prompts/sx/one_task.txt.", error.detail)
        self.assertEqual(error.fix, ("file", f.repo / "prompts" / "examples" / "sx" / "one_task.txt"))
        (f.repo / "templates" / "meta.yaml").unlink()
        f.write("templates/meta.example.yaml", "example")
        meta = [c for c in find(f.run(), "Task files", status=health.ERROR) if "metadata_config" in c.label][0]
        self.assertIn("Copy templates/meta.example.yaml to templates/meta.yaml.", meta.detail)

    def test_formatting_libreoffice_is_informational_and_the_template_must_exist(self):
        f = self.fixture()
        with mock.patch.object(health, "LIBREOFFICE_PATHS", ()):
            libre = find(f.run(which=lambda name: None), "Formatting", "LibreOffice")[0]
        self.assertEqual(libre.status, health.INFO)          # never an error: s1b format does not need it
        self.assertIn("s1b format writes the .odt without it", libre.detail)
        self.assertEqual(find(f.run(), "Formatting", "LibreOffice")[0].status, health.OK)
        self.assertEqual(find(f.run(), "Formatting", "Book template")[0].status, health.OK)
        (f.repo / "templates" / "book.odt").unlink()
        template = find(f.run(), "Formatting", "Book template")[0]
        self.assertEqual(template.status, health.ERROR)
        self.assertIn("templates/book.odt, named in templates/format_styles.yaml, does not exist", template.detail)
        f.write("templates/format_styles.yaml", "styles: {}\n")
        self.assertIn("names no template", find(f.run(), "Formatting", "Book template")[0].detail)

    def test_policy_drift_is_a_warning_that_points_at_the_candidates_screen(self):
        f = self.fixture()
        self.assertEqual(find(f.run(), "Policy")[0].status, health.OK)
        f.write("docs/vsm.md", VSM_OK.replace("travel.", "travel and food."))
        policy = find(f.run(), "Policy")[0]
        self.assertEqual((policy.status, policy.fix), (health.WARNING, ("screen", "candidates")))
        self.assertIn("OUT OF SYNC", policy.detail)

    def test_a_missing_data_folder_is_a_warning_not_an_error(self):
        f = self.fixture()
        shutil.rmtree(f.repo / "newsletter")
        missing = find(f.run(), "Data folders", status=health.WARNING)
        self.assertEqual([c.label for c in missing], ["newsletter_dir"])
        self.assertIn("created when first needed", missing[0].detail)

    def test_config_fixes_open_the_config_file(self):
        f = self.fixture(config(llm={"provider": "mistral"}))
        self.assertEqual(find(f.run(), "LLM provider", "Model")[0].fix, ("file", f.cfg_path))

    def test_the_summary_counts_errors_and_warnings(self):
        checks = [health.Check("a", "b", health.ERROR), health.Check("a", "c", health.ERROR), health.Check("a", "d", health.WARNING),
                  health.Check("a", "e", health.OK)]
        self.assertEqual(health.summary(checks), "2 errors, 1 warning.")
        self.assertEqual(health.summary(checks[:1]), "1 error.")


class ProviderTableDriftTests(unittest.TestCase):
    def test_the_environment_variables_listed_are_the_ones_the_provider_factory_reads(self):
        source = (REPO_ROOT / "providers" / "__init__.py").read_text(encoding="utf-8")
        used = set(re.findall(r'"([A-Z][A-Z0-9]+_[A-Z0-9_]+)"', source))
        listed = {n for names in (*health.LLM_ENV.values(), *health.TRANSLATION_ENV.values()) for n in names}
        self.assertEqual(listed, used)

    def test_the_providers_listed_are_the_ones_the_factory_dispatches_on(self):
        source = (REPO_ROOT / "providers" / "__init__.py").read_text(encoding="utf-8")
        dispatched = set(re.findall(r'name == "([a-z-]+)"', source))
        self.assertEqual(dispatched, set(health.LLM_ENV) | set(health.TRANSLATION_ENV))


class RealRepositoryTests(unittest.TestCase):
    def test_the_operators_environment_reports_no_errors(self):
        config_file = REPO_ROOT / "config.yaml"
        if not config_file.exists():
            self.skipTest("personal file missing: config.yaml")
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
        ctx = Context(repo_root=REPO_ROOT, config_path=config_file, config={})
        checks = health.run_checks(ctx)
        missing_files = [c for c in checks if c.area == "Task files" and c.status == health.ERROR]
        if missing_files:
            self.skipTest(f"personal file missing: {missing_files[0].detail}")
        self.assertEqual([(c.area, c.label, c.detail) for c in checks if c.status == health.ERROR], [])


try:
    import textual  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Textual is not installed (uv sync --extra tui)")

from textual.widgets import Button, DataTable, Static  # noqa: E402
from tui.app import PipelineApp  # noqa: E402
from tui.screens.health import HealthScreen  # noqa: E402

SIZE = (170, 100)


async def settle(pilot, times=3):
    for _ in range(times):
        await pilot.pause()


class HealthScreenTests(GuardedCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ws.update("llm", provider="mistral", model="mistral-medium-latest")
        self.ctx = Context.load(self.ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)

    async def open(self, pilot, app, environ):
        await pilot.press("h")
        await settle(pilot)
        screen = app.screen
        self.assertIsInstance(screen, HealthScreen)
        screen.environ = environ
        screen.refresh_state()
        await settle(pilot)
        return screen

    def cells(self, screen):
        table = screen.query_one("#health-checks", DataTable)
        return [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]

    async def test_a_missing_key_shows_as_an_error_with_a_summary(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app, {})
            row = next(r for r in self.cells(screen) if r[1] == "MISTRAL_API_KEY")
            self.assertEqual(row[2:], ["ERROR", "missing (add it to .env)"])
            self.assertRegex(screen.query_one("#health-summary", Static).render().plain, r"\d+ errors?")

    async def test_a_key_value_in_the_environment_never_appears_on_the_screen(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app, {"MISTRAL_API_KEY": SECRET})
            row = next(r for r in self.cells(screen) if r[1] == "MISTRAL_API_KEY")
            self.assertEqual(row[2:], ["OK", "set"])
            self.assertNotIn(SECRET, repr(self.cells(screen)))
            self.assertNotIn(SECRET, app.export_screenshot())    # everything drawn, as a picture
            table = screen.query_one("#health-checks", DataTable)
            table.move_cursor(row=[r[1] for r in self.cells(screen)].index("MISTRAL_API_KEY"))
            await settle(pilot)
            self.assertNotIn(SECRET, screen.query_one("#health-detail", Static).render().plain)
            self.assertNotIn(SECRET, app.export_screenshot())

    async def test_the_fix_button_opens_the_file_or_goes_to_the_screen(self):
        self.ws.update("llm", model="")   # llm.model unset: the fix is the config file
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app, {"MISTRAL_API_KEY": "x", "DEEPL_API_KEY": "x"})
            table = screen.query_one("#health-checks", DataTable)
            fix = screen.query_one("#health-fix", Button)
            names = [(r[0], r[1]) for r in self.cells(screen)]
            table.move_cursor(row=names.index(("LLM provider", "Model")))
            await settle(pilot)
            self.assertFalse(fix.disabled)
            with mock.patch("tui.screens.health.open_path") as opened:
                await pilot.click("#health-fix")
                await settle(pilot)
            opened.assert_called_once_with(self.ws.config_path)

    async def test_a_line_with_nothing_to_fix_has_no_fix_button(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app, {"MISTRAL_API_KEY": "x", "DEEPL_API_KEY": "x"})
            table = screen.query_one("#health-checks", DataTable)
            names = [(r[0], r[1]) for r in self.cells(screen)]
            table.move_cursor(row=names.index(("LLM provider", "Provider")))
            await settle(pilot)
            self.assertTrue(screen.query_one("#health-fix", Button).disabled)

    async def test_f5_checks_again(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app, {})
            before = next(r for r in self.cells(screen) if r[1] == "MISTRAL_API_KEY")[2]
            screen.environ = {"MISTRAL_API_KEY": "x"}
            await pilot.press("f5")
            await settle(pilot)
            after = next(r for r in self.cells(screen) if r[1] == "MISTRAL_API_KEY")[2]
            self.assertEqual((before, after), ("ERROR", "OK"))


if __name__ == "__main__":
    unittest.main()
