"""ADR 023: no test in the suite reads or writes the operator's real data folders.

Named `test_zz_*` so it sorts last: `harness.SUITE_GUARD` took its listing when the
harness was first imported (before any test ran), and this compares it with the
listing after every other test module has run.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402


class RealDataUntouchedTests(unittest.TestCase):
    def test_real_data_folders_are_unchanged_after_the_suite(self):
        harness.SUITE_GUARD.assert_unchanged()


if __name__ == "__main__":
    unittest.main()
