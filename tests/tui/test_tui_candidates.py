"""ADR 023, T7.1 — candidates under S5 evaluation. The state helpers need no Textual; the
screen tests skip without it.
"""
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402
from harness import EXAMPLE_LAUNCHER, GuardedCase, Workspace, fixture, root_group  # noqa: E402
from lib import paths  # noqa: E402
from tui.services import files, state  # noqa: E402
from tui.services.context import Context  # noqa: E402

logging.getLogger("asyncio").setLevel(logging.ERROR)

CALIBRATION = [
    {"candidate": "gamma", "agent_verdict": "No encaja", "human_decision": "declined", "date": "2026-08-01"},
    {"candidate": "old1", "agent_verdict": "Encaja", "human_decision": "acquired", "date": "2026-07-01"},
    {"candidate": "old2", "agent_verdict": "Encaja", "human_decision": "declined", "date": "2026-07-02"},
    {"candidate": "old3", "agent_verdict": "Encaja", "human_decision": "acquired", "date": "2026-07-03"},
]


def seed(ws):
    """alpha: not evaluated; beta: evaluated (Encaja); gamma: decided; delta: verdict without a tag;
    empty: a brief with nothing in it yet."""
    root = paths.candidates_root(ws.config)
    briefs, verdicts = root / "s1a" / "briefs", root / "s5" / "evaluate"
    briefs.mkdir(parents=True)
    verdicts.mkdir(parents=True)
    for slug in ("alpha", "beta", "gamma", "delta"):
        (briefs / f"{slug}.txt").write_text(fixture("tiny_candidate_brief.txt").read_text(encoding="utf-8"), encoding="utf-8")
    (briefs / "empty.txt").write_text("  \n", encoding="utf-8")
    sample = fixture("verdict_sample.txt").read_text(encoding="utf-8")
    (verdicts / "beta.txt").write_text(sample, encoding="utf-8")
    (verdicts / "gamma.txt").write_text(sample.replace("Encaja", "No encaja"), encoding="utf-8")
    (verdicts / "delta.txt").write_text("## Veredicto\nEncaja, pero sin etiqueta.\n", encoding="utf-8")
    (root / "calibration.yaml").write_text(yaml.safe_dump(CALIBRATION, allow_unicode=True), encoding="utf-8")
    return root


class CandidateStateTests(GuardedCase):
    def test_states_verdicts_and_decisions(self):
        with Workspace() as ws:
            rows = {r.slug: r for r in state.candidate_rows(seed(ws))}
            self.assertEqual(list(rows), ["alpha", "beta", "delta", "empty", "gamma"])
            self.assertEqual((rows["alpha"].state, rows["alpha"].verdict, rows["alpha"].decision),
                             ("not evaluated", None, None))
            self.assertEqual((rows["beta"].state, rows["beta"].verdict, rows["beta"].decision), ("evaluated", "Encaja", None))
            self.assertEqual((rows["gamma"].state, rows["gamma"].verdict, rows["gamma"].decision, rows["gamma"].decided_on),
                             ("decided", "No encaja", "declined", "2026-08-01"))
            self.assertTrue(rows["empty"].brief_empty)
            self.assertFalse(rows["alpha"].brief_empty)

    def test_a_verdict_file_without_the_tag_shows_a_question_mark(self):
        with Workspace() as ws:
            rows = {r.slug: r for r in state.candidate_rows(seed(ws))}
            self.assertEqual((rows["delta"].state, rows["delta"].verdict), ("evaluated", state.NO_VERDICT_TAG))
            self.assertEqual(state.NO_VERDICT_TAG, "?")

    def test_the_latest_decision_for_a_candidate_wins(self):
        with Workspace() as ws:
            root = seed(ws)
            entries = CALIBRATION + [{"candidate": "gamma", "agent_verdict": "No encaja", "human_decision": "deferred",
                                      "date": "2026-09-01"}]
            (root / "calibration.yaml").write_text(yaml.safe_dump(entries), encoding="utf-8")
            gamma = next(r for r in state.candidate_rows(root) if r.slug == "gamma")
            self.assertEqual((gamma.decision, gamma.decided_on), ("deferred", "2026-09-01"))

    def test_calibration_counts_match_the_file(self):
        with Workspace() as ws:
            counts = state.calibration_counts(seed(ws))
            self.assertEqual([(c.verdict, c.decision, c.count) for c in counts],
                             [("Encaja", "acquired", 2), ("Encaja", "declined", 1), ("No encaja", "declined", 1)])

    def test_nothing_yet_is_empty_not_an_error(self):
        with Workspace() as ws:
            root = ws.candidates_dir
            self.assertEqual((state.candidate_rows(root), state.calibration_counts(root)), ([], []))
            (root / "calibration.yaml").write_text("- [unclosed", encoding="utf-8")
            self.assertEqual(state.calibration_counts(root), [])

    def test_listing_changes_nothing(self):
        with Workspace() as ws:
            root = seed(ws)
            before = sorted((p.name, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file())
            state.candidate_rows(root)
            state.calibration_counts(root)
            self.assertEqual(before, sorted((p.name, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()))


class CandidateFileTests(GuardedCase):
    def test_slug_rules_and_duplicates(self):
        with Workspace() as ws:
            ctx = Context.load(ws.config_path)
            seed(ws)
            self.assertIsNone(files.candidate_slug_problem(ctx, "la-croisiere-jaune"))
            for bad in ("", "Two Words", "-x", "x_y"):
                self.assertIsNotNone(files.candidate_slug_problem(ctx, bad), repr(bad))
            self.assertIn("already exists", files.candidate_slug_problem(ctx, "alpha"))

    def test_create_brief_makes_an_empty_file_and_never_overwrites(self):
        with Workspace() as ws:
            ctx = Context.load(ws.config_path)
            path = files.create_brief(ctx, "new-one")
            self.assertEqual((path, path.read_text(encoding="utf-8")), (ws.candidates_dir / "s1a" / "briefs" / "new-one.txt", ""))
            path.write_text("Mi descripcion.", encoding="utf-8")
            files.create_brief(ctx, "new-one")
            self.assertEqual(path.read_text(encoding="utf-8"), "Mi descripcion.")


try:
    import textual  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Textual is not installed (uv sync --extra tui)")

from textual.widgets import DataTable, Input, Select, Static  # noqa: E402
from tui.app import PipelineApp  # noqa: E402
from tui.screens.candidates import CandidatesScreen  # noqa: E402
from tui.screens.run import RunScreen  # noqa: E402
from tui.widgets.run_button import RunButton  # noqa: E402

SIZE = (170, 100)


async def settle(pilot, times=3):
    for _ in range(times):
        await pilot.pause()


class CandidatesScreenTests(GuardedCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)

    async def open(self, pilot, app):
        await pilot.press("c")
        await settle(pilot)
        self.assertIsInstance(app.screen, CandidatesScreen)
        return app.screen

    def rows(self, screen):
        table = screen.query_one("#cand-list", DataTable)
        return {str(table.get_row_at(i)[0]): [str(c) for c in table.get_row_at(i)] for i in range(table.row_count)}

    async def select(self, pilot, screen, slug):
        table = screen.query_one("#cand-list", DataTable)
        table.move_cursor(row=table.get_row_index(slug))
        await settle(pilot)

    async def finish(self, pilot, app, *, confirm=False):
        await settle(pilot)
        self.assertIsInstance(app.screen, RunScreen)
        if confirm:
            self.assertEqual(app.screen.state, "confirm")
            await pilot.click("#run-go")
            await settle(pilot)
        await app.session.wait()
        await settle(pilot, 5)
        await pilot.press("escape")
        await settle(pilot)

    async def test_the_list_shows_states_verdicts_decisions_and_the_reminder(self):
        seed(self.ws)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            rows = self.rows(screen)
            self.assertEqual(rows["alpha"][1:], ["not evaluated", "-", "-", "-"])
            self.assertEqual(rows["beta"][1:], ["evaluated", "Encaja", "-", "-"])
            self.assertEqual(rows["gamma"][1:], ["decided", "No encaja", "declined", "2026-08-01"])
            self.assertEqual(rows["delta"][1], "evaluated")
            self.assertEqual(rows["delta"][2], "?")   # no <verdict> tag: a question mark, not a crash
            reminder = screen.query_one("#cand-reminder", Static).render().plain
            self.assertIn("plain prose", reminder)
            self.assertIn("copyright status", reminder)
            self.assertIn("Label: value", reminder)

    async def test_the_calibration_view_matches_the_file(self):
        seed(self.ws)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            table = screen.query_one("#cand-calibration", DataTable)
            self.assertEqual([[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)],
                             [["Encaja", "acquired", "2"], ["Encaja", "declined", "1"], ["No encaja", "declined", "1"]])

    async def test_open_brief_and_open_verdict_target_the_right_files(self):
        root = seed(self.ws)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await self.select(pilot, screen, "alpha")
            self.assertTrue(screen.query_one("#cand-open-verdict").disabled)   # nothing evaluated yet
            await self.select(pilot, screen, "beta")
            with mock.patch("tui.screens.candidates.open_path") as opened:
                await pilot.click("#cand-open-brief")
                await pilot.click("#cand-open-verdict")
                await settle(pilot)
            self.assertEqual([c.args[0] for c in opened.call_args_list],
                             [root / "s1a" / "briefs" / "beta.txt", root / "s5" / "evaluate" / "beta.txt"])

    async def test_an_empty_brief_cannot_be_evaluated_and_an_unevaluated_one_cannot_be_decided(self):
        seed(self.ws)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            evaluate, record = screen.query_one("#cand-evaluate", RunButton), screen.query_one("#cand-record", RunButton)
            await self.select(pilot, screen, "empty")
            self.assertTrue(evaluate.disabled)
            self.assertIn("brief is empty", evaluate.tooltip)
            await self.select(pilot, screen, "alpha")
            self.assertFalse(evaluate.disabled)
            self.assertTrue(record.disabled)
            self.assertEqual(record.tooltip, "Evaluate it first")
            await self.select(pilot, screen, "beta")
            self.assertFalse(record.disabled)
            self.assertEqual(str(evaluate.label), "Evaluate again")

    async def test_evaluating_again_warns_that_the_verdict_is_replaced(self):
        seed(self.ws)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await self.select(pilot, screen, "beta")
            await pilot.click("#cand-evaluate")
            await settle(pilot)
            text = app.screen.query_one("#run-confirm", Static).render().plain
            self.assertIn("has already been evaluated", text)
            self.assertIn("Provider calls: about 1", text)
            await pilot.click("#run-close")

    async def test_new_candidate_then_evaluate_then_record_a_decision_ends_in_a_calibration_entry(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            screen.query_one("#cand-new-slug", Input).value = "winter-pamirs"
            await settle(pilot)
            with mock.patch("tui.screens.candidates.open_path") as opened:
                await pilot.click("#cand-new")
                await self.finish(pilot, app)
            brief = self.ws.candidates_dir / "s1a" / "briefs" / "winter-pamirs.txt"
            self.assertEqual(brief.read_text(encoding="utf-8"), "")
            opened.assert_called_once_with(brief)          # opened for writing in the editor
            self.assertEqual(self.rows(screen)["winter-pamirs"][1], "not evaluated")
            self.assertEqual(screen.query_one("#cand-new-slug", Input).value, "")

            await self.select(pilot, screen, "winter-pamirs")
            self.assertTrue(screen.query_one("#cand-evaluate", RunButton).disabled)   # still empty
            brief.write_text(fixture("tiny_candidate_brief.txt").read_text(encoding="utf-8"), encoding="utf-8")
            screen.refresh_state()
            await settle(pilot)
            await self.select(pilot, screen, "winter-pamirs")
            await pilot.click("#cand-evaluate")
            await self.finish(pilot, app, confirm=True)
            self.assertEqual(self.rows(screen)["winter-pamirs"][1:3], ["evaluated", "Encaja"])

            await self.select(pilot, screen, "winter-pamirs")
            await pilot.click("#cand-record")
            await settle(pilot)
            self.assertIn("Choose a decision", screen.query_one("#cand-decision-error", Static).render().plain)
            screen.query_one("#cand-decision-select", Select).value = "acquired"
            await settle(pilot)
            await pilot.click("#cand-record")
            await self.finish(pilot, app)

            entries = yaml.safe_load((self.ws.candidates_dir / "calibration.yaml").read_text(encoding="utf-8"))
            self.assertEqual([(e["candidate"], e["agent_verdict"], e["human_decision"]) for e in entries],
                             [("winter-pamirs", "Encaja", "acquired")])
            self.assertEqual(self.rows(screen)["winter-pamirs"][1:4], ["decided", "Encaja", "acquired"])
            calibration = screen.query_one("#cand-calibration", DataTable)
            self.assertEqual([str(c) for c in calibration.get_row_at(0)], ["Encaja", "acquired", "1"])

    async def test_a_free_text_decision_wins_over_the_dropdown(self):
        seed(self.ws)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await self.select(pilot, screen, "beta")
            screen.query_one("#cand-decision-select", Select).value = "declined"
            screen.query_one("#cand-decision-text", Input).value = "wait for the 2027 list"
            await settle(pilot)
            with mock.patch.object(app, "begin_run") as begin:
                await pilot.click("#cand-record")
                await settle(pilot)
            self.assertEqual(begin.call_args.args[0].argv[-3:], ["record-decision", "beta", "wait for the 2027 list"])

    async def test_duplicate_and_invalid_names_are_rejected_without_running_anything(self):
        seed(self.ws)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            for typed, expected in (("alpha", "already exists"), ("Two Words", "lowercase"), ("", "Enter a name")):
                screen.query_one("#cand-new-slug", Input).value = typed
                await settle(pilot)
                await pilot.click("#cand-new")
                await settle(pilot)
                self.assertIs(app.screen, screen)
                self.assertIn(expected, screen.query_one("#cand-new-error", Static).render().plain, typed)
            self.assertIsNone(app.session)


if __name__ == "__main__":
    unittest.main()
