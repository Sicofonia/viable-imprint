"""`pipeline.py` with every task redirected to `prompts/examples/` and the test
fixtures, and `httpx.get` stubbed to serve `tiny_feed.xml` (ADR 023, T0.3).

Not a test (the name does not match `test_*`). Tests and the UI's own tests run
it exactly like `pipeline.py`: `python tests/example_pipeline.py --config X s2 status ...`.
The redirect happens before `pipeline` is imported, because the manifest-driven
command groups are built at import time. A subprocess cannot be patched from the
test process, which is why the stubs live here.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import harness  # noqa: E402
from lib import task_loader  # noqa: E402

task_loader.load_system_tasks = harness.example_tasks

import httpx  # noqa: E402


def _stub_get(url, **kwargs):
    return httpx.Response(200, content=harness.fixture("tiny_feed.xml").read_bytes(),
                          request=httpx.Request("GET", url))


httpx.get = _stub_get

import pipeline  # noqa: E402

if __name__ == "__main__":
    pipeline.cli()
