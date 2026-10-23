"""ADR 023, T3.1 — the print prep screen over `s1c`. The arithmetic needs no Textual;
the screen tests skip without it.
"""
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness  # noqa: E402
from harness import EXAMPLE_LAUNCHER, GuardedCase, Workspace, root_group  # noqa: E402
from tui.services import commands, printprep  # noqa: E402
from tui.services.context import Context  # noqa: E402

logging.getLogger("asyncio").setLevel(logging.ERROR)


class ArithmeticTests(unittest.TestCase):
    def test_the_cheat_sheet_cover_example(self):
        # trim 304 (front + back, no spine), spine 12, bleed 3, height 229 -> 322 x 235 mm
        self.assertEqual(printprep.expected_cover_size(304, 229, 12, 3), (322, 235))
        self.assertEqual(printprep.describe_size(322, 235), "322 x 235 mm")

    def test_defaults_are_no_spine_and_three_millimetres_of_bleed(self):
        self.assertEqual(printprep.expected_cover_size(304, 229), (310, 235))

    def test_fractions_are_shown_without_noise(self):
        self.assertEqual(printprep.describe_size(322.5, 235.25, "mm"), "322.5 x 235.25 mm")
        self.assertEqual(printprep.describe_size(322.0, 235.0, "pt"), "322 x 235 pt")

    def test_the_preview_follows_the_typed_values(self):
        values = {"trim_w": "304", "trim_h": "229", "spine_w": "12", "bleed": "3", "unit": "mm"}
        text = printprep.cover_preview(values)
        self.assertIn("322 x 235 mm", text)
        self.assertIn("(304 + 12 spine + 2 x 3 bleed) x (229 + 2 x 3 bleed)", text)
        values.update(spine_w=None, bleed=None)  # defaults apply
        self.assertIn("310 x 235 mm", printprep.cover_preview(values))
        values["unit"] = "pt"
        self.assertIn("310 x 235 pt", printprep.cover_preview(values))

    def test_the_preview_asks_for_missing_or_bad_numbers(self):
        self.assertIn("Enter the cover width", printprep.cover_preview({"trim_h": "229"}))
        self.assertIn("'wide' is not a number", printprep.cover_preview({"trim_w": "wide", "trim_h": "229"}))

    def test_the_default_output_name_never_equals_the_input(self):
        for name in ("cover.pdf", "interior_print.pdf", "My Book.PDF", "noext", "a/b/c.pdf"):
            out = printprep.default_output(name)
            self.assertNotEqual(Path(out), Path(name), name)
        self.assertEqual(Path(printprep.default_output("a/b/cover.pdf")), Path("a/b/cover_print.pdf"))
        self.assertEqual(Path(printprep.default_output("interior_print.pdf")), Path("interior_print_print.pdf"))
        self.assertEqual(Path(printprep.default_output("noext")), Path("noext_print.pdf"))


class TypedValuesTests(GuardedCase):
    """`argv_for(..., keep_typed=True)`: numbers the operator typed stay in the command even
    when they equal the default, so the line matches the cheat sheet's examples."""

    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path)
        from tui.services.command_model import build_model
        self.model = {s.name: s for s in build_model(root_group())}

    def test_a_typed_default_is_kept_and_an_untouched_one_is_not(self):
        spec = self.model["s1c set-cover"]
        values = {"input_file": "cover.pdf", "output_file": "cover_print.pdf", "trim_w": "304", "trim_h": "229",
                  "spine_w": "12", "bleed": "3", "unit": "mm"}
        kept = commands.argv_for(self.ctx, spec, values, keep_typed=True)
        self.assertEqual(kept[-2:], ["--bleed", "3"])
        plain = commands.argv_for(self.ctx, spec, values)
        self.assertNotIn("--bleed", plain)
        values["bleed"] = None
        self.assertNotIn("--bleed", commands.argv_for(self.ctx, spec, values, keep_typed=True))
        self.assertNotIn("--unit", kept)  # a dropdown can't tell typed from default: left to the default


try:
    import textual  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Textual is not installed (uv sync --extra tui)")

from textual.widgets import Button, Input, Static, TabbedContent  # noqa: E402
from tui.app import PipelineApp  # noqa: E402
from tui.screens.print_prep import PrintPrepScreen  # noqa: E402
from tui.screens.run import RunScreen  # noqa: E402
from tui.widgets.param_form import ParamForm  # noqa: E402

SIZE = (150, 100)  # the cover form is tall; clicks need the button on screen


async def settle(pilot, times=3):
    for _ in range(times):
        await pilot.pause()


def make_pdf(path: Path, width_mm: float, height_mm: float, pages: int = 2) -> Path:
    import pikepdf
    pdf = pikepdf.Pdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(width_mm * 72 / 25.4, height_mm * 72 / 25.4))
    pdf.save(path)
    return path


class PrintPrepScreenTests(GuardedCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.ws = Workspace().__enter__()
        self.addCleanup(self.ws.__exit__, None, None, None)
        self.ctx = Context.load(self.ws.config_path, pipeline_script=EXAMPLE_LAUNCHER)

    async def open(self, pilot, app):
        await pilot.press("p")
        await settle(pilot)
        self.assertIsInstance(app.screen, PrintPrepScreen)
        return app.screen

    def form(self, screen, tab):
        return screen.query_one(f"#form-{tab}", ParamForm)

    async def fill(self, pilot, screen, tab, **values):
        screen.query_one(TabbedContent).active = f"tab-{tab}"
        await settle(pilot)
        form = self.form(screen, tab)
        for name, value in values.items():
            form.set_value(name, value)
        await settle(pilot)

    def shown(self, screen, tab):
        text = screen.query_one(f"#hint-{tab}").text.rstrip()
        return "s1c " + text.split(" s1c ", 1)[-1] if " s1c " in text else text

    async def test_the_workflow_note_from_the_cheat_sheet_is_on_the_screen(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            note = screen.query_one("#pp-note", Static).render().plain
            self.assertIn("export the PDF from LibreOffice by hand", note)
            self.assertIn("Nothing here touches the run-state ledger", note)
            self.assertIn("/TrimBox and /BleedBox", note)

    async def test_the_commands_match_the_cheat_sheets_examples(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await self.fill(pilot, screen, "check", input_file="interior.pdf", trim_w="152", trim_h="229")
            self.assertEqual(self.shown(screen, "check"), "s1c check interior.pdf --trim-w 152 --trim-h 229")
            await self.fill(pilot, screen, "interior", input_file="interior.pdf", trim_w="152", trim_h="229")
            self.assertEqual(self.shown(screen, "interior"),
                             "s1c set-interior interior.pdf interior_print.pdf --trim-w 152 --trim-h 229")
            await self.fill(pilot, screen, "cover", input_file="cover.pdf", trim_w="304", trim_h="229",
                            spine_w="12", bleed="3")
            self.assertEqual(self.shown(screen, "cover"),
                             "s1c set-cover cover.pdf cover_print.pdf --trim-w 304 --trim-h 229 --spine-w 12 --bleed 3")

    async def test_the_cover_form_shows_the_expected_pdf_size_as_you_type(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            size = lambda: screen.query_one("#size-cover", Static).render().plain
            await self.fill(pilot, screen, "cover")
            self.assertIn("Enter the cover width", size())
            await self.fill(pilot, screen, "cover", trim_w="304", trim_h="229", spine_w="12", bleed="3")
            self.assertIn("322 x 235 mm", size())
            await self.fill(pilot, screen, "cover", bleed="5")
            self.assertIn("326 x 239 mm", size())

    async def test_the_output_defaults_to_input_print_and_follows_the_input_until_edited(self):
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await self.fill(pilot, screen, "interior", input_file=str(self.ws.base / "book.pdf"))
            out = screen.query_one("#form-interior", ParamForm).query_one("#param-output_file", Input)
            self.assertEqual(Path(out.value), self.ws.base / "book_print.pdf")
            await self.fill(pilot, screen, "interior", input_file=str(self.ws.base / "other.pdf"))
            self.assertEqual(Path(out.value), self.ws.base / "other_print.pdf")  # still following
            out.value = str(self.ws.base / "chosen-name.pdf")  # the operator picks a name
            await settle(pilot)
            await self.fill(pilot, screen, "interior", input_file=str(self.ws.base / "third.pdf"))
            self.assertEqual(Path(out.value), self.ws.base / "chosen-name.pdf")

    async def test_an_output_equal_to_the_input_is_refused(self):
        pdf = make_pdf(self.ws.base / "same.pdf", 152, 229)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await self.fill(pilot, screen, "interior", input_file=str(pdf), output_file=str(pdf),
                            trim_w="152", trim_h="229")
            await pilot.click("#run-interior")
            await settle(pilot)
            self.assertIs(app.screen, screen)
            self.assertIn("must differ", screen.query_one("#hint-interior").text)
            self.assertIsNone(app.session)

    async def run_tab(self, pilot, app, tab):
        await settle(pilot, 6)  # a tab that was just switched to needs a layout pass before it can be clicked
        await pilot.click(f"#run-{tab}")
        await settle(pilot)
        self.assertIsInstance(app.screen, RunScreen)
        self.assertEqual(app.screen.state, "running" if app.session.running else "done")
        await app.session.wait()
        await settle(pilot, 5)

    async def test_set_cover_then_check_the_result_shows_explicit_boxes(self):
        cover = make_pdf(self.ws.base / "cover.pdf", 322, 235, pages=1)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await self.fill(pilot, screen, "cover", input_file=str(cover), trim_w="304", trim_h="229",
                            spine_w="12", bleed="3")
            await self.run_tab(pilot, app, "cover")
            printed = self.ws.base / "cover_print.pdf"
            self.assertTrue(printed.exists())
            self.assertTrue(any("TrimBox" in line for line in app.session.lines))
            await pilot.press("escape")  # close the log
            await settle(pilot)

            check = screen.query_one("#again-cover", Button)
            self.assertFalse(check.disabled)  # offered once the command has produced a file
            await pilot.click("#again-cover")
            await settle(pilot)
            self.assertEqual(screen.query_one(TabbedContent).active, "tab-check")
            self.assertEqual(Path(self.form(screen, "check").values()["input_file"]), printed)
            await self.run_tab(pilot, app, "check")
            lines = "\n".join(app.session.lines)
            self.assertRegex(lines, r"TrimBox.*\(explicit\)")
            self.assertRegex(lines, r"BleedBox.*\(explicit\)")

    async def test_the_check_after_set_interior_carries_the_trim_size_over(self):
        interior = make_pdf(self.ws.base / "interior.pdf", 152, 229)
        app = PipelineApp(self.ctx, root_group())
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open(pilot, app)
            await self.fill(pilot, screen, "interior", input_file=str(interior), trim_w="152", trim_h="229")
            self.assertTrue(screen.query_one("#again-interior", Button).disabled)  # nothing produced yet
            await self.run_tab(pilot, app, "interior")
            await pilot.press("escape")
            await settle(pilot)
            await pilot.click("#again-interior")
            await settle(pilot)
            values = self.form(screen, "check").values()
            self.assertEqual((Path(values["input_file"]), values["trim_w"], values["trim_h"]),
                             (self.ws.base / "interior_print.pdf", "152", "229"))
            await self.run_tab(pilot, app, "check")
            self.assertRegex("\n".join(app.session.lines), r"TrimBox.*\(explicit\)")


if __name__ == "__main__":
    unittest.main()
