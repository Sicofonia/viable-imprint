"""ADR 023, T0.5 — dashboard formatters moved to lib/formatting.py, and the proof
that `s3 dashboard` output did not change when they moved.

The golden files in tests/fixtures/dashboard_*.golden.txt were captured from the
code *before* the move, on the `harness.seed_portfolio()` fixture (line endings
normalised to LF so the comparison does not depend on the platform).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
from harness import GuardedCase, Workspace, fixture, run_cli, seed_portfolio  # noqa: E402
from lib import formatting  # noqa: E402


class FormatCostTests(unittest.TestCase):
    def test_none_is_a_dash(self):
        self.assertEqual(formatting.format_cost(None), "-")

    def test_sub_cent_costs_keep_six_decimals(self):  # ADR 005 point 9
        self.assertEqual(formatting.format_cost(0.0001), "$0.000100")
        self.assertEqual(formatting.format_cost(0.009999), "$0.009999")

    def test_a_cent_or_more_uses_three_decimals(self):
        self.assertEqual(formatting.format_cost(0.01), "$0.010")
        self.assertEqual(formatting.format_cost(1.5), "$1.500")

    def test_zero_is_shown_as_a_real_zero_not_a_dash(self):
        self.assertEqual(formatting.format_cost(0.0), "$0.000000")


class FormatDurationTests(unittest.TestCase):
    def test_seconds_and_minutes(self):
        self.assertEqual(formatting.format_duration(0), "0s")
        self.assertEqual(formatting.format_duration(59.4), "59s")
        self.assertEqual(formatting.format_duration(60), "1m 00s")
        self.assertEqual(formatting.format_duration(105), "1m 45s")

    def test_none_counts_as_zero(self):
        self.assertEqual(formatting.format_duration(None), "0s")


class FormatMarginTests(unittest.TestCase):
    def test_positive_negative_and_missing(self):
        self.assertEqual(formatting.format_margin(48.5), "$48.50")
        self.assertEqual(formatting.format_margin(-1.5), "-$1.50")
        self.assertEqual(formatting.format_margin(0), "$0.00")
        self.assertEqual(formatting.format_margin(None), "-")


class FormatRevenueTests(unittest.TestCase):
    def test_empty_is_a_dash(self):
        self.assertEqual(formatting.format_revenue({}), "-")

    def test_currencies_are_listed_separately_in_alphabetical_order(self):  # ADR 011 point 6
        self.assertEqual(formatting.format_revenue({"USD": 5, "EUR": 10.256}), "10.26 EUR, 5.00 USD")


class FormatSpanTests(unittest.TestCase):
    def test_no_runs(self):
        self.assertEqual(formatting.format_span(None, None), "no runs yet")

    def test_same_day_is_in_progress(self):
        self.assertEqual(formatting.format_span("2026-09-01T10:00:00+00:00", "2026-09-01T18:00:00+00:00"),
                         "2026-09-01 -> 2026-09-01 (in progress)")

    def test_days_are_pluralised(self):
        self.assertEqual(formatting.format_span("2026-09-01T00:00:00", "2026-09-02T00:00:00"),
                         "2026-09-01 -> 2026-09-02 (1 day)")
        self.assertEqual(formatting.format_span("2026-09-01T00:00:00", "2026-09-04T00:00:00"),
                         "2026-09-01 -> 2026-09-04 (3 days)")


class DashboardOutputUnchangedTests(GuardedCase):
    def assert_matches_golden(self, name, *args):
        with Workspace() as ws:
            seed_portfolio(ws)
            code, output = run_cli(ws, "s3", "dashboard", *args, example=True)
        self.assertEqual(code, 0, output)
        golden = fixture(f"dashboard_{name}.golden.txt").read_bytes().decode("utf-8")
        # Both sides normalised: a Windows checkout may give the golden file CRLF endings.
        self.assertEqual(output.replace("\r\n", "\n"), golden.replace("\r\n", "\n"))

    def test_portfolio_view(self):
        self.assert_matches_golden("portfolio")

    def test_book_view_with_flags_and_revenue(self):
        self.assert_matches_golden("book-d", "book-d")

    def test_book_view_with_a_task_that_has_no_cost(self):
        self.assert_matches_golden("book-b", "book-b")

    def test_book_view_without_flags(self):
        self.assert_matches_golden("book-a", "book-a")


if __name__ == "__main__":
    unittest.main()
