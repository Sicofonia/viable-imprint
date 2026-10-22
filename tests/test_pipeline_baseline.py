"""ADR 023, T0.4 — pipeline regression baseline on fake providers.

Exercises every system before any UI exists: the net that proves later work
changed nothing, and a check on the argument shapes the UI will build. No
network, no key, no quota.

Two styles, as in the ADR's tier table:
- in-process: `task_loader.run_task()` with `example_task()` dicts (prompts from
  `prompts/examples/`, facts from the fixtures), so it runs on a clean clone;
- CLI: `pipeline.py` as a subprocess. The example launcher is used wherever the
  case would otherwise need the operator's personal prompts; the one case about
  the real `format` template skips with the missing file's path.
"""
import contextlib
import datetime
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
from harness import (FIXTURE_ISBN, GuardedCase, REPO_ROOT, Workspace, fixture, ledger,  # noqa: E402
                     make_book, run_cli)
from lib import manifest, paths, task_loader  # noqa: E402


def run_example(ws, root, system, name, input_file=None, **kwargs):
    """One task in-process, quietly. Returns the output path."""
    with contextlib.redirect_stdout(io.StringIO()):
        return task_loader.run_task(root, ws.config, system, harness.example_task(system, name),
                                    input_file=input_file, **kwargs)


def cli(case, ws, *args, expect=0):
    """The example-mode CLI; asserts the exit code and returns the output."""
    code, output = run_cli(ws, *args, example=True)
    case.assertEqual(code, expect, f"{' '.join(map(str, args))} exited {code}:\n{output}")
    return output


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def source_of(root: Path) -> Path:
    return root / "s1b" / "source" / "tiny_source.txt"


# ---------------------------------------------------------------------------
# S1B
# ---------------------------------------------------------------------------

class S1BTests(GuardedCase):
    def test_cleanup_translate_ortho_copyedit_write_under_the_language_folder(self):
        with Workspace() as ws:
            root = make_book(ws, "tiny")
            cleaned = run_example(ws, root, "s1b", "cleanup", source_of(root))
            translated = run_example(ws, root, "s1b", "translate", cleaned)
            ortho = run_example(ws, root, "s1b", "ortho", translated)
            copyedit = run_example(ws, root, "s1b", "copyedit", ortho)

            self.assertEqual(cleaned, root / "s1b" / "cleaned" / "tiny_source.txt")
            self.assertEqual(translated, root / "s1b" / "translated" / "es" / "tiny_source.txt")
            self.assertEqual(ortho, root / "s1b" / "ortho" / "es" / "tiny_source.txt")
            self.assertEqual(copyedit, root / "s1b" / "copyedit" / "es" / "tiny_source.txt")
            # Each fake stage embeds the first 200 characters of its input (the previous
            # fake's boilerplate), so the original text survives only the first stage.
            self.assertIn("THE ROAD TO KASHGAR", read(cleaned))
            self.assertEqual(read(translated), read(cleaned))  # the fake translator returns its input

            entries = ledger(root)
            self.assertEqual(set(entries), {"s1b.cleanup", "s1b.translate", "s1b.ortho", "s1b.copyedit"})
            self.assertTrue(all(e["status"] == "done" for e in entries.values()))
            self.assertEqual(entries["s1b.translate"]["usage"], {"characters": len(read(cleaned))})
            self.assertEqual(manifest.load(root)["translation_pair"], "EN→ES")

    def test_format_produces_an_odt(self):
        harness.require_personal_files("s1b", "format")
        with Workspace() as ws:
            root = make_book(ws, "tiny")
            text = root / "s1b" / "copyedit" / "es" / "tiny_source.txt"
            text.parent.mkdir(parents=True)
            text.write_text("CAPITULO I.\nEL CAMINO.\n\nPrimer parrafo.\n\nSegundo parrafo.\n", encoding="utf-8")
            output = run_example(ws, root, "s1b", "format", text)
            self.assertEqual(output.suffix, ".odt")
            self.assertGreater(output.stat().st_size, 0)


# ---------------------------------------------------------------------------
# S1D book tasks
# ---------------------------------------------------------------------------

class S1DBookTests(GuardedCase):
    def build_brief(self, ws):
        root = make_book(ws, "tiny")
        text = source_of(root)
        for name in ("cleanup", "translate", "ortho", "copyedit"):
            text = run_example(ws, root, "s1b" if name != "brief" else "s1d", name, text)
        return root, run_example(ws, root, "s1d", "brief", text)

    def test_brief_expansions_and_metadata(self):
        with Workspace() as ws:
            root, brief = self.build_brief(ws)
            self.assertEqual(brief, root / "s1d" / "brief" / "es" / "tiny_source.txt")

            outputs = {name: run_example(ws, root, "s1d", name, brief)
                       for name in ("synopsis", "story-map", "one-pager", "press-dossier",
                                    "trailer-storyboard", "goodreads-profile", "metadata")}
            for name, path in outputs.items():
                self.assertEqual(path, root / "s1d" / name / "es" / "tiny_source.txt", name)
                self.assertTrue(read(path).strip(), name)

            metadata = read(outputs["metadata"])
            self.assertIn("Descripcion de prueba generada por el proveedor simulado", metadata)  # the fake <overview>
            self.assertIn("**Author**: A. Fixture Author", metadata)
            self.assertIn(f"**ISBN**: {FIXTURE_ISBN}", metadata)

            dossier = read(outputs["press-dossier"]).rstrip()
            self.assertTrue(dossier.endswith("- **Email**: contact@example.invalid"), dossier[-120:])

            entries = ledger(root)
            self.assertNotIn("usage", entries["s1d.metadata"])  # no provider call
            self.assertIn("usage", entries["s1d.synopsis"])


# ---------------------------------------------------------------------------
# S2 book orchestration (CLI)
# ---------------------------------------------------------------------------

class S2BookTests(GuardedCase):
    def test_status_on_a_fresh_book(self):
        with Workspace() as ws:
            make_book(ws, "tiny")
            output = cli(self, ws, "s2", "status", "tiny")
            self.assertRegex(output, r"\[ \] cleanup\s+ready")
            self.assertRegex(output, r"\[ \] translate\s+blocked . waiting on s1b\.cleanup")
            self.assertRegex(output, r"\[ \] brief\s+blocked . waiting on s1b\.copyedit")
            self.assertEqual(output.count(" ready"), 1)

    def test_step_runs_exactly_one_task(self):
        with Workspace() as ws:
            root = make_book(ws, "tiny")
            output = cli(self, ws, "s2", "run", "tiny", "--step")
            self.assertIn("Done: 1, Failed: 0", output)
            self.assertEqual(set(ledger(root)), {"s1b.cleanup"})

    def test_only_s1b_completes_s1b_and_s1d_only_follows(self):
        with Workspace() as ws:
            root = make_book(ws, "tiny")
            cli(self, ws, "s2", "run", "tiny", "--only", "s1b")
            entries = ledger(root)
            for name in ("cleanup", "translate", "ortho", "copyedit"):
                self.assertEqual(entries[f"s1b.{name}"]["status"], "done", name)
            self.assertFalse([k for k in entries if k.startswith("s1d.")])
            if not all(p.exists() for p in harness.personal_files("s1b", "format")):
                self.assertEqual(entries["s1b.format"]["status"], "failed")  # personal template absent

            cli(self, ws, "s2", "run", "tiny", "--only", "s1d")
            self.assertTrue(all(entries_["status"] == "done" for k, entries_ in ledger(root).items()
                                if k.startswith("s1d.")))

    def test_rerunning_cleanup_marks_downstream_tasks_stale(self):
        with Workspace() as ws:
            root = make_book(ws, "tiny")
            cli(self, ws, "s2", "run", "tiny", "--only", "s1b")
            cli(self, ws, "s1b", "cleanup", source_of(root))
            entries = ledger(root)
            for name in ("translate", "ortho", "copyedit"):
                self.assertEqual(entries[f"s1b.{name}"]["status"], "stale", name)
                self.assertEqual(entries[f"s1b.{name}"]["invalidated_by"], "s1b.cleanup", name)
            self.assertEqual(entries["s1b.cleanup"]["status"], "done")
            self.assertRegex(cli(self, ws, "s2", "status", "tiny"), r"\[~\] translate\s+stale \(ready to rerun\)")

    def test_a_failure_is_recorded_blocks_dependents_and_is_retried_next_run(self):
        with Workspace(llm={"fail": True}) as ws:
            root = make_book(ws, "tiny")
            output = cli(self, ws, "s2", "run", "tiny", "--only", "s1b", expect=1)
            self.assertIn("Failed: 1", output)
            entry = ledger(root)["s1b.cleanup"]
            self.assertEqual(entry["status"], "failed")
            self.assertIn("configured to fail", entry["error"])
            self.assertNotIn("s1b.translate", ledger(root))
            self.assertRegex(cli(self, ws, "s2", "status", "tiny"), r"\[!\] cleanup\s+failed \(ready to retry\)")

            ws.update("llm", fail=False)
            cli(self, ws, "s2", "run", "tiny", "--only", "s1b")
            self.assertEqual(ledger(root)["s1b.cleanup"]["status"], "done")

    def test_independent_siblings_still_run_after_a_failure(self):
        with Workspace() as ws:
            root = make_book(ws, "tiny")
            cli(self, ws, "s2", "run", "tiny", "--only", "s1b")
            cli(self, ws, "s2", "run", "tiny", "--only", "s1d", "--step")  # just `brief`
            ws.update("llm", fail=True)
            output = cli(self, ws, "s2", "run", "tiny", "--only", "s1d", expect=1)
            entries = ledger(root)
            self.assertEqual(entries["s1d.metadata"]["status"], "done")  # no LLM, ran despite the failures
            self.assertEqual(entries["s1d.synopsis"]["status"], "failed")
            self.assertIn("Done: 1, Failed: 6", output)

            ws.update("llm", fail=False)
            cli(self, ws, "s2", "run", "tiny", "--only", "s1d")
            self.assertEqual(entries["s1d.synopsis"]["status"], "failed")
            self.assertEqual(ledger(root)["s1d.synopsis"]["status"], "done")


# ---------------------------------------------------------------------------
# S2 periodic pipelines (CLI)
# ---------------------------------------------------------------------------

class S2PeriodicTests(GuardedCase):
    def test_homeostat_run_renders_the_html_under_the_homeostat_folder(self):
        with Workspace() as ws:
            self.assertIn("never run", cli(self, ws, "s2", "homeostat", "status"))
            output = cli(self, ws, "s2", "homeostat", "run")
            self.assertIn("Homeostat chain complete.", output)
            pages = list(ws.homeostat_dir.rglob("*.html"))
            self.assertEqual(len(pages), 1)
            self.assertIn("Homeostato", read(pages[0]))
            status = cli(self, ws, "s2", "homeostat", "status")
            for stage in ("s5.homeostat-scan", "s5.homeostat ", "s5.homeostat-render"):
                self.assertIn(f"[x] {stage}", status)

    def test_homeostat_step_runs_one_stage(self):
        with Workspace() as ws:
            cli(self, ws, "s2", "homeostat", "run", "--step")
            self.assertEqual(set(ledger(ws.homeostat_dir)), {"s5.homeostat-scan"})

    def test_newsletter_run_records_the_featured_names_and_warns_on_a_repeat(self):
        with Workspace() as ws:
            notes = ws.newsletter_dir / "notes"
            notes.mkdir()
            (notes / f"{datetime.date.today().isoformat()[:7]}.txt").write_text("Una nota del mes.", encoding="utf-8")

            first = cli(self, ws, "s2", "newsletter", "run")
            self.assertIn("Newsletter chain complete.", first)
            self.assertNotIn("appears to repeat", first)
            explorers = yaml.safe_load(read(ws.newsletter_dir / "featured_explorers.yaml"))
            dishes = yaml.safe_load(read(ws.newsletter_dir / "featured_dishes.yaml"))
            self.assertEqual([e["name"] for e in explorers], ["Exploradora de Prueba"])
            self.assertEqual([d["name"] for d in dishes], ["Plato de Prueba"])
            final = next((ws.newsletter_dir / "s1d" / "newsletter-track").rglob("combined.txt"))
            self.assertNotIn("<explorer>", read(final))  # tags stripped from the sendable copy

            second = cli(self, ws, "s2", "newsletter", "run")
            self.assertIn("appears to repeat a previous explorer", second)
            self.assertIn("appears to repeat a previous dish", second)
            self.assertEqual(len(yaml.safe_load(read(ws.newsletter_dir / "featured_explorers.yaml"))), 1)

            status = cli(self, ws, "s2", "newsletter", "status")
            for stage in ("newsletter-scan", "newsletter ", "newsletter-track"):
                self.assertIn(f"[x] s1d.{stage}", status)

    def test_a_failed_first_stage_stops_the_chain(self):
        with Workspace(llm={"fail": True}) as ws:
            cli(self, ws, "s2", "homeostat", "run", expect=1)
            entries = ledger(ws.homeostat_dir)
            self.assertEqual(entries["s5.homeostat-scan"]["status"], "done")
            self.assertEqual(entries["s5.homeostat"]["status"], "failed")
            self.assertNotIn("s5.homeostat-render", entries)


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------

class S3Tests(GuardedCase):
    def test_ingest_each_fixture_auto_detected_and_forced(self):
        with Workspace() as ws:
            make_book(ws, "ing", source=False)
            make_book(ws, "kdp", source=False)
            for slug in ("ing", "kdp"):
                cli(self, ws, "book", "set-isbn", slug, FIXTURE_ISBN)

            out = cli(self, ws, "s3", "sales-ingest", "ing", fixture("tiny_ingramspark.csv"))
            self.assertIn("Format: ingramspark", out)
            self.assertIn(f"Rows matching ISBN {FIXTURE_ISBN}: 1", out)
            self.assertIn("New entries recorded: 1", out)
            sale = manifest.load(ws.book("ing"))["sales"][0]
            self.assertEqual((sale["units"], sale["revenue"], sale["currency"]), (12, 48.6, "USD"))

            out = cli(self, ws, "s3", "sales-ingest", "kdp", fixture("tiny_kdp.csv"), "--format", "kdp")
            self.assertIn("Format: kdp", out)
            sale = manifest.load(ws.book("kdp"))["sales"][0]
            self.assertEqual((sale["units"], sale["revenue"]), (5, 13.0))
            self.assertEqual((sale["period_start"], sale["period_end"]), ("2026-08-03", "2026-08-17"))

    def test_ingesting_the_same_csv_twice_adds_nothing(self):
        with Workspace() as ws:
            make_book(ws, "ing", source=False)
            cli(self, ws, "book", "set-isbn", "ing", FIXTURE_ISBN)
            cli(self, ws, "s3", "sales-ingest", "ing", fixture("tiny_ingramspark.csv"))
            again = cli(self, ws, "s3", "sales-ingest", "ing", fixture("tiny_ingramspark.csv"))
            self.assertIn("New entries recorded: 0", again)
            self.assertIn("skipped as duplicates): 1", again)
            self.assertEqual(len(manifest.load(ws.book("ing"))["sales"]), 1)

    def test_ingest_errors_name_the_known_formats_and_the_missing_isbn(self):
        with Workspace() as ws:
            make_book(ws, "ing", source=False)
            odd = ws.base / "odd.csv"
            odd.write_text("colour,shape\nred,round\n", encoding="utf-8")
            self.assertIn("ISBN", cli(self, ws, "s3", "sales-ingest", "ing", fixture("tiny_kdp.csv"), expect=1))
            cli(self, ws, "book", "set-isbn", "ing", FIXTURE_ISBN)
            out = cli(self, ws, "s3", "sales-ingest", "ing", odd, expect=1)
            self.assertIn("Could not recognize", out)
            self.assertIn("ingramspark, kdp", out)

    def test_dashboard_shows_revenue_cost_and_deviation_flags(self):
        with Workspace() as ws:
            seed_slugs = harness.seed_portfolio(ws)
            portfolio = cli(self, ws, "s3", "dashboard")
            self.assertIn(f"Portfolio: {len(seed_slugs)} books", portfolio)
            self.assertRegex(portfolio, r"book-d\s+2/13\s+\$0\.100 !")  # cost flag marker
            self.assertIn("48.60 USD", portfolio)
            self.assertIn("20.00 EUR", portfolio)
            self.assertIn("Flagged: book-d — API cost $0.100 is 10.0x", portfolio)
            self.assertNotRegex(portfolio, r"book-a\s+\S+\s+\$0\.010000 !")

            book = cli(self, ws, "s3", "dashboard", "book-d")
            self.assertIn("1m 40s !", book)  # duration outlier marker
            self.assertIn("Revenue: 48.60 USD", book)
            self.assertIn("Reported margin (revenue minus tracked API cost only): $48.50", book)
            self.assertIn("Flagged: s1b.cleanup — 1m 40s is 10.0x", book)

            clean = cli(self, ws, "s3", "dashboard", "book-a")
            self.assertNotIn("Flagged", clean)


# ---------------------------------------------------------------------------
# S4
# ---------------------------------------------------------------------------

def stub_feed(url, **kwargs):
    return httpx.Response(200, content=fixture("tiny_feed.xml").read_bytes(), request=httpx.Request("GET", url))


class S4Tests(GuardedCase):
    def scan(self, ws, root):
        with mock.patch("engines.feed_scan.httpx.get", stub_feed):
            return run_example(ws, root, "s4", "scan")

    def test_scan_then_a_second_scan_reports_nothing_new(self):
        with Workspace() as ws:
            root = paths.intelligence_root(ws.config)
            first = self.scan(ws, root)
            self.assertEqual(first.name, "combined.txt")
            text = read(first)
            self.assertIn("Tiny test feed", text)
            self.assertIn("New edition of a Central Asian travel classic announced", text)

            second = self.scan(ws, root)  # same dated folder: overwritten with the "nothing new" text
            self.assertIn("Sin elementos nuevos", read(second))

    def test_briefing_content_strategy_and_article_draft(self):
        with Workspace() as ws:
            root = paths.intelligence_root(ws.config)
            scan = root / "s4" / "scan" / "2026-10-01" / "combined.txt"
            scan.parent.mkdir(parents=True)
            scan.write_text(read(fixture("tiny_combined_scan.txt")), encoding="utf-8")

            briefing = run_example(ws, root, "s4", "briefing", scan)
            strategy = run_example(ws, root, "s4", "content-strategy", briefing)
            draft = run_example(ws, root, "s1d", "article-draft", strategy)

            self.assertEqual(briefing, root / "s4" / "briefing" / "2026-10-01" / "combined.txt")
            self.assertEqual(strategy, root / "s4" / "content-strategy" / "2026-10-01" / "combined.txt")
            self.assertEqual(draft, root / "s1d" / "article-draft" / "2026-10-01" / "combined.txt")
            self.assertIn("Fecha: Mon, 05 Oct 2026", read(briefing))  # excerpt of the scan fixture


# ---------------------------------------------------------------------------
# S5
# ---------------------------------------------------------------------------

class S5Tests(GuardedCase):
    def test_candidate_evaluate_record_decision_and_log_decision(self):
        with Workspace() as ws:
            out = cli(self, ws, "candidate", "new", "winter-pamirs")
            brief = ws.candidates_dir / "s1a" / "briefs" / "winter-pamirs.txt"
            self.assertIn(str(brief), out)
            brief.write_text(read(fixture("tiny_candidate_brief.txt")), encoding="utf-8")

            cli(self, ws, "s5", "evaluate", brief)
            verdict = ws.candidates_dir / "s5" / "evaluate" / "winter-pamirs.txt"
            self.assertIn("<verdict>Encaja</verdict>", read(verdict))

            cli(self, ws, "candidate", "record-decision", "winter-pamirs", "acquired")
            entries = yaml.safe_load(read(ws.candidates_dir / "calibration.yaml"))
            self.assertEqual(len(entries), 1)
            self.assertEqual((entries[0]["candidate"], entries[0]["agent_verdict"], entries[0]["human_decision"]),
                             ("winter-pamirs", "Encaja", "acquired"))

            missing = cli(self, ws, "candidate", "record-decision", "never-evaluated", "declined", expect=1)
            self.assertIn("No verdict found", missing)

            cli(self, ws, "s5", "log-decision", "cost vs. coverage", "scan weekly")
            decisions = yaml.safe_load(read(ws.homeostat_dir / "decisions.yaml"))
            self.assertEqual((decisions[0]["tension"], decisions[0]["decision"]), ("cost vs. coverage", "scan weekly"))

    def test_check_policy_sync_is_clean_on_the_committed_files_and_read_only(self):
        watched = [REPO_ROOT / "prompts" / "s5" / "policy_evaluation_task.txt", REPO_ROOT / "docs" / "vsm.md"]
        before = [p.read_bytes() for p in watched]
        with Workspace() as ws:
            self.assertIn("In sync", cli(self, ws, "s5", "check-policy-sync"))
        self.assertEqual(before, [p.read_bytes() for p in watched])  # `--update` is never run here


# ---------------------------------------------------------------------------
# S1C
# ---------------------------------------------------------------------------

def make_pdf(path: Path, width_mm: float, height_mm: float, pages: int = 2) -> Path:
    import pikepdf
    pdf = pikepdf.Pdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(width_mm * 72 / 25.4, height_mm * 72 / 25.4))
    pdf.save(path)
    return path


def box_mm(page, name):
    return [round(float(v) * 25.4 / 72, 2) for v in page[name]]


class S1CTests(GuardedCase):
    def test_interior_check_set_interior_check(self):
        import pikepdf
        with Workspace() as ws:
            interior = make_pdf(ws.base / "interior.pdf", 152, 229)
            out = cli(self, ws, "s1c", "check", interior, "--trim-w", "152", "--trim-h", "229")
            self.assertIn("inherited from MediaBox", out)

            printed = ws.base / "interior_print.pdf"
            cli(self, ws, "s1c", "set-interior", interior, printed, "--trim-w", "152", "--trim-h", "229")
            out = cli(self, ws, "s1c", "check", printed, "--trim-w", "152", "--trim-h", "229")
            self.assertIn("(explicit)", out)
            with pikepdf.open(printed) as pdf:
                self.assertTrue(all("/TrimBox" in page for page in pdf.pages))

    def test_cover_matches_the_cheat_sheet_worked_example(self):
        import pikepdf
        with Workspace() as ws:
            cover = make_pdf(ws.base / "cover.pdf", 322, 235, pages=1)  # (304 + 12 + 2*3) x (229 + 2*3)
            printed = ws.base / "cover_print.pdf"
            cli(self, ws, "s1c", "set-cover", cover, printed,
                "--trim-w", "304", "--trim-h", "229", "--spine-w", "12", "--bleed", "3")
            with pikepdf.open(printed) as pdf:
                page = pdf.pages[0]
                trim, bleed = box_mm(page, "/TrimBox"), box_mm(page, "/BleedBox")
            self.assertAlmostEqual(trim[2] - trim[0], 316, delta=0.05)
            self.assertAlmostEqual(trim[3] - trim[1], 229, delta=0.05)
            self.assertAlmostEqual(bleed[2] - bleed[0], 322, delta=0.05)
            self.assertAlmostEqual(bleed[3] - bleed[1], 235, delta=0.05)
            self.assertIn("(explicit)", cli(self, ws, "s1c", "check", printed))


# ---------------------------------------------------------------------------
# Known issue, pinned (ADR 023 Verification notes, item 2)
# ---------------------------------------------------------------------------

class DirectScanRootTests(GuardedCase):
    """`s5 homeostat-scan` and `s1d newsletter-scan` run directly resolve their
    root with `paths.intelligence_root()`, not `homeostat/` or `newsletter/`.
    Not fixed by ADR 023. If a fix lands, these tests fail: then delete them and
    the explorer's warning for these two commands."""

    def test_homeostat_scan_run_directly_writes_into_intelligence(self):
        with Workspace() as ws:
            cli(self, ws, "s5", "homeostat-scan")
            self.assertTrue((ws.intelligence_dir / "s5" / "homeostat-scan").is_dir())
            self.assertFalse((ws.homeostat_dir / "s5").exists())

    def test_newsletter_scan_run_directly_writes_into_intelligence(self):
        with Workspace() as ws:
            cli(self, ws, "s1d", "newsletter-scan")
            self.assertTrue((ws.intelligence_dir / "s1d" / "newsletter-scan").is_dir())
            self.assertTrue((ws.intelligence_dir / "notes").is_dir())
            self.assertFalse((ws.newsletter_dir / "s1d").exists())


if __name__ == "__main__":
    unittest.main()
