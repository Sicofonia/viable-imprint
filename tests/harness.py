"""Shared helpers for tests that need a throwaway workspace (ADR 023, T0.3).

Plain `unittest` helpers, no new dependency. Every test that touches pipeline
data gets its own temporary books/intelligence/candidates/homeostat/newsletter
folders, written as absolute paths into a generated test config, so no test
reads or writes the operator's real data. Tests run with the repository root as
the working directory (manifest, prompt and template paths are repo-relative);
`Workspace` switches to it on entry and restores the previous directory on exit.

Two ways to exercise the CLI:
- `run_cli(ws, ...)` runs the real `pipeline.py`. It needs the operator's
  personal prompts, so a test using it should call `require_personal_files()`.
- `run_cli(ws, ..., example=True)` runs `tests/example_pipeline.py`, the same
  CLI with every task redirected to `prompts/examples/` and the fixtures (see
  `example_tasks()`), so it works on a clean clone.
"""
import contextlib
import copy
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
PIPELINE_SCRIPT = REPO_ROOT / "pipeline.py"
EXAMPLE_LAUNCHER = REPO_ROOT / "tests" / "example_pipeline.py"

DATA_DIR_KEYS = ("books_dir", "intelligence_dir", "candidates_dir", "homeostat_dir", "newsletter_dir")

# Fixed on purpose: tests compute expected costs by hand from these.
PRICING_LLM = {"prompt_per_million": 1.0, "completion_per_million": 2.0}
PRICING_TRANSLATION = {"per_million_characters": 20.0}

FIXTURE_ISBN = "9781234567897"


def fixture(name: str) -> Path:
    return FIXTURES / name


def default_config(base: Path) -> dict:
    return {
        "llm": {"provider": "fake", "model": "fake-1", "pricing": dict(PRICING_LLM)},
        "translation": {
            "provider": "fake", "source_lang": "EN", "target_lang": "ES",
            "fake": {"pricing": dict(PRICING_TRANSLATION)},
        },
        "s3": {"deviation": {"cost_multiplier": 3.0, "duration_multiplier": 3.0}},
        "books_dir": str(base / "books"),
        "intelligence_dir": str(base / "intelligence"),
        "candidates_dir": str(base / "candidates"),
        "homeostat_dir": str(base / "homeostat"),
        "newsletter_dir": str(base / "newsletter"),
    }


class Workspace:
    """Temporary data folders plus a generated config, removed on exit."""

    def __init__(self, **sections):
        # e.g. Workspace(llm={"fail": True}) merges into the default llm block.
        self._sections = sections
        self._previous_cwd = None
        self._tmp = None

    def __enter__(self):
        self._previous_cwd = os.getcwd()
        os.chdir(REPO_ROOT)
        self._tmp = tempfile.TemporaryDirectory(prefix="vi-test-")
        self.base = Path(self._tmp.name).resolve()
        self.config = default_config(self.base)
        for section, values in self._sections.items():
            self.config[section].update(values)
        for key in DATA_DIR_KEYS:
            Path(self.config[key]).mkdir(parents=True)
        self.config_path = self.base / "config.yaml"
        self.write_config()
        return self

    def __exit__(self, *exc):
        os.chdir(self._previous_cwd)
        self._tmp.cleanup()

    def write_config(self):
        self.config_path.write_text(yaml.safe_dump(self.config, allow_unicode=True), encoding="utf-8")

    def update(self, section: str, **values):
        """Change a config section after entry (e.g. `ws.update("llm", fail=True)`)."""
        self.config[section].update(values)
        self.write_config()

    @property
    def books_dir(self) -> Path:
        return Path(self.config["books_dir"])

    @property
    def intelligence_dir(self) -> Path:
        return Path(self.config["intelligence_dir"])

    @property
    def candidates_dir(self) -> Path:
        return Path(self.config["candidates_dir"])

    @property
    def homeostat_dir(self) -> Path:
        return Path(self.config["homeostat_dir"])

    @property
    def newsletter_dir(self) -> Path:
        return Path(self.config["newsletter_dir"])

    def book(self, slug: str) -> Path:
        return self.books_dir / slug


def run_cli(ws: Workspace, *args, example: bool = False, timeout: float = 180) -> tuple:
    """Run `pipeline.py --config <test config> <args>` as a subprocess from the
    repository root. Returns (returncode, combined stdout+stderr)."""
    script = EXAMPLE_LAUNCHER if example else PIPELINE_SCRIPT
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.run(
        [sys.executable, str(script), "--config", str(ws.config_path), *map(str, args)],
        cwd=REPO_ROOT, env=env, capture_output=True, timeout=timeout,
    )
    output = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    return proc.returncode, output


def make_book(ws: Workspace, slug: str, *, source: bool = True) -> Path:
    """`init` through the CLI, then copy `tiny_source.txt` into `s1b/source/`."""
    code, output = run_cli(ws, "init", slug)
    if code != 0:
        raise AssertionError(f"init failed:\n{output}")
    root = ws.book(slug)
    if source:
        (root / "s1b" / "source" / "tiny_source.txt").write_text(
            fixture("tiny_source.txt").read_text(encoding="utf-8"), encoding="utf-8")
    return root


def ledger(root: Path) -> dict:
    """A folder's run-state ledger (manifest.yaml's `tasks:` block)."""
    with open(root / "manifest.yaml", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("tasks", {})


# ---------------------------------------------------------------------------
# Example tasks: the real manifests, redirected to files that exist on a clean
# clone. The manifests are read as plain YAML (never through `task_loader`),
# so patching `task_loader.load_system_tasks` with `example_tasks` cannot recurse.
# ---------------------------------------------------------------------------

def _real_tasks(system: str) -> list:
    path = REPO_ROOT / "systems" / system / "tasks.yaml"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("tasks", [])


def _redirect(system: str, task: dict) -> dict:
    task = copy.deepcopy(task)
    for key in ("prompt", "reduce_prompt"):
        if task.get(key):
            task[key] = f"prompts/examples/{system}/{Path(task[key]).name}"
    if task.get("metadata_config"):
        task["metadata_config"] = "tests/fixtures/tiny_marketing_metadata.yaml"
    if task.get("sources_config"):
        task["sources_config"] = "tests/fixtures/tiny_sources.yaml"
    return task


def example_tasks(system_name: str) -> list:
    """Drop-in for `task_loader.load_system_tasks`."""
    return [_redirect(system_name, t) for t in _real_tasks(system_name)]


def example_task(system: str, name: str, **overrides) -> dict:
    """One task dict from `tasks.yaml`, redirected to `prompts/examples/` and the fixtures."""
    for task in example_tasks(system):
        if task["name"] == name:
            task.update(overrides)
            return task
    raise KeyError(f"{system}.{name} is not in systems/{system}/tasks.yaml")


@contextlib.contextmanager
def example_manifests():
    """In-process equivalent of the example launcher: every system's tasks come
    from `example_tasks()`, for `orchestrator.run_book`, `run_periodic`, etc."""
    from lib import task_loader
    with mock.patch.object(task_loader, "load_system_tasks", example_tasks):
        yield


def personal_files(system: str, name: str) -> list:
    """Every gitignored file the real task needs: its prompts, metadata and
    sources configs, and for `format` the style config and the template it names."""
    task = next(t for t in _real_tasks(system) if t["name"] == name)
    needed = [REPO_ROOT / task[k] for k in ("prompt", "reduce_prompt", "metadata_config", "sources_config")
              if task.get(k)]
    if task.get("style_config"):
        style = REPO_ROOT / task["style_config"]
        needed.append(style)
        if style.exists():
            with open(style, encoding="utf-8") as f:
                template = (yaml.safe_load(f) or {}).get("template")
            if template:
                needed.append(REPO_ROOT / template)
    return needed


def require_personal_files(system: str, name: str) -> None:
    """Skip the calling test, naming the first missing personal file."""
    for path in personal_files(system, name):
        if not path.exists():
            raise unittest.SkipTest(f"personal file missing: {path.relative_to(REPO_ROOT)}")


# ---------------------------------------------------------------------------
# Real-data guard
# ---------------------------------------------------------------------------

class RealDataGuard:
    """Listing (path, size, mtime) of the operator's real data folders, to prove
    a test or the whole suite never touched them."""

    _DEFAULTS = {"books_dir": "books", "intelligence_dir": "intelligence", "candidates_dir": "candidates",
                 "homeostat_dir": "homeostat", "newsletter_dir": "newsletter"}

    def __init__(self):
        config = {}
        real = REPO_ROOT / "config.yaml"
        if real.exists():
            with open(real, encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        self.roots = [(REPO_ROOT / config.get(key, default)).resolve() for key, default in self._DEFAULTS.items()]
        self.before = self.listing()

    def listing(self) -> dict:
        found = {}
        for root in self.roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for filename in filenames:
                    path = Path(dirpath) / filename
                    stat = path.stat()
                    found[str(path)] = (stat.st_size, stat.st_mtime_ns)
        return found

    def assert_unchanged(self):
        now = self.listing()
        if now != self.before:
            added = sorted(set(now) - set(self.before))
            removed = sorted(set(self.before) - set(now))
            changed = sorted(p for p in set(now) & set(self.before) if now[p] != self.before[p])
            raise AssertionError(f"real data folders changed: added={added} removed={removed} changed={changed}")


# Taken when this module is first imported, i.e. before any test has run
# (unittest imports every test module before running the first test).
SUITE_GUARD = RealDataGuard()
