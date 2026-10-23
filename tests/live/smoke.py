"""Live smoke test (ADR 023, T8.2): real providers, tiny inputs, only when you run it.

    uv run python tests/live/smoke.py --scope core            # 4 LLM calls
    uv run python tests/live/smoke.py --scope full            # about 16 LLM calls, translation skipped
    uv run python tests/live/smoke.py --scope full --with-translate
    uv run python tests/live/smoke.py --scope core --dry-run  # print the plan, call nothing

It uses your real config and .env, but every data folder (books, intelligence, candidates,
homeostat, newsletter) is a temporary one, so nothing of yours is read or written. Every command
line is built by tui/services/commands.py, so this also exercises the UI's argv building against
real providers.

Before anything runs it prints the provider, model, planned number of calls, the translation
provider and its expected wait, and the cost ceiling, and asks for confirmation (unless --yes).
It refuses to start if an input file exceeds 1,500 characters, stops as soon as the cost recorded
in the workspace ledgers exceeds PIPELINE_LIVE_MAX_USD (default 0.10), and gives every command a
time limit, so it cannot run or wait forever.

`full` skips translation by default: the temporary config selects the fake translation provider
for that run, so the real `s2 run` chain still completes. --with-translate uses the configured one
(eTranslation is asynchronous and can take minutes, even for a paragraph).

Not named test_*: `unittest discover` never runs it. tests/test_live_smoke.py tests it on fakes.
Exit codes: 0 all passed, 1 a command failed, 2 refused to start, 3 cost ceiling exceeded,
4 declined, 130 interrupted.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib import manifest, task_loader  # noqa: E402
from tui.services import commands, health  # noqa: E402
from tui.services.command_model import build_model  # noqa: E402
from tui.services.context import DATA_DIRS, Context  # noqa: E402
from tui.services.files import dated_outputs  # noqa: E402
from tui.services.output import chain_summary  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures"
MAX_INPUT_CHARS = 1500
DEFAULT_MAX_USD = 0.10
# Known free-tier request quotas per day, to warn when a run would eat a big share of one.
DAILY_QUOTAS = {"google-aistudio": 20}
QUOTA_WARN_FRACTION = 0.25
STEP_TIMEOUT = 900.0                       # seconds, per command
TRANSLATION_TIMEOUT = 2 * 60 * 60 + 600.0   # eTranslation polls for up to 120 minutes
BOOK = "smoke-book"
CANDIDATE = "smoke-candidate"
NOTES_LINE = "Una nota breve de prueba para el boletin."
WAITS = {"etranslation": "asynchronous: minutes, up to 2 hours, even for a paragraph (a re-run resumes it)",
         "deepl": "seconds", "fake": "none (the fake provider)"}


@dataclass
class Step:
    name: str
    path: list
    values: Callable          # (Workspace) -> dict of form values; called when the step runs
    llm_calls: int
    translation_calls: int = 0
    timeout: float = STEP_TIMEOUT
    chain: bool = False       # an `s2 ... run` chain: its output ends with a Done/Failed block


@dataclass
class Result:
    step: str
    passed: bool
    seconds: float
    note: str = ""
    cost_after: float = 0.0


class Workspace:
    """Temporary data folders and a config that points at them; everything else is the real config."""

    def __init__(self, real_config: dict, with_translate: bool, base: Path = None):
        self.base = Path(base or tempfile.mkdtemp(prefix="vi-live-")).resolve()
        self.logs = self.base / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.config = dict(real_config)
        for key, default in DATA_DIRS.items():
            self.config[key] = str(self.base / default)
            Path(self.config[key]).mkdir(parents=True, exist_ok=True)
        if not with_translate:
            self.config["translation"] = {**(real_config.get("translation") or {}), "provider": "fake"}
        self.config_path = self.base / "config.yaml"
        self.config_path.write_text(yaml.safe_dump(self.config, allow_unicode=True), encoding="utf-8")

    @property
    def book(self) -> Path:
        return Path(self.config["books_dir"]) / BOOK

    @property
    def intelligence(self) -> Path:
        return Path(self.config["intelligence_dir"])

    @property
    def candidate_brief(self) -> Path:
        return Path(self.config["candidates_dir"]) / "s1a" / "briefs" / f"{CANDIDATE}.txt"

    def roots(self) -> list:
        books = Path(self.config["books_dir"])
        book_roots = [p for p in sorted(books.iterdir()) if p.is_dir()] if books.is_dir() else []
        return book_roots + [Path(self.config[k]) for k in ("intelligence_dir", "candidates_dir", "homeostat_dir", "newsletter_dir")]

    def total_cost(self) -> float:
        """Sum of the `cost_usd` every task recorded in any ledger here (tasks with no cost add nothing)."""
        total = 0.0
        for root in self.roots():
            for entry in manifest.load(root).get("tasks", {}).values():
                total += entry.get("cost_usd") or 0.0
        return total


# -- the plan ------------------------------------------------------------------------------

def _llm_tasks(systems, only_book_scoped=True) -> int:
    return sum(1 for system in systems for t in task_loader.load_system_tasks(system)
               if t["engine"] == "llm_text" and (t.get("book_scoped", True) or not only_book_scoped))


def _translation_tasks() -> int:
    return sum(1 for system in ("s1b", "s1d") for t in task_loader.load_system_tasks(system)
               if t["engine"] == "translation" and t.get("book_scoped", True))


def _periodic_llm(tasks) -> int:
    by_key = {(s, t["name"]): t for s in ("s1d", "s5") for t in task_loader.load_system_tasks(s)}
    return sum(1 for key in tasks if by_key[key]["engine"] == "llm_text")


def _latest(ws: Workspace, system: str, stage: str) -> str:
    found = dated_outputs(ws.intelligence, system, stage)
    return str(found[0].path) if found else str(ws.intelligence / system / stage / "(missing)" / "combined.txt")


def plan(scope: str, with_translate: bool) -> list:
    """The commands to run, with the provider calls each is expected to make. Call counts come
    from the manifests, so a task added or removed there changes the plan by itself."""
    from lib import orchestrator
    cleaned = lambda ws: str(ws.book / "s1b" / "cleaned" / "tiny_source.txt")
    brief = lambda ws: str(ws.book / "s1d" / "brief" / "tiny_source.txt")
    evaluate = Step("s5 evaluate", ["s5", "evaluate"], lambda ws: {"input_file": str(ws.candidate_brief)}, 1)
    if scope == "core":
        return [
            Step("s1b cleanup", ["s1b", "cleanup"],
                 lambda ws: {"input_file": str(ws.book / "s1b" / "source" / "tiny_source.txt")}, 1),
            Step("s1d brief", ["s1d", "brief"], lambda ws: {"input_file": cleaned(ws)}, 1),
            Step("s1d synopsis", ["s1d", "synopsis"], lambda ws: {"input_file": brief(ws)}, 1),
            evaluate,
        ]
    book_llm = _llm_tasks(("s1b", "s1d"))
    return [
        Step(f"s2 run {BOOK}", ["s2", "run"], lambda ws: {"book_slug": BOOK}, book_llm,
             translation_calls=_translation_tasks() if with_translate else 0,
             timeout=TRANSLATION_TIMEOUT if with_translate else STEP_TIMEOUT, chain=True),
        Step("s4 briefing", ["s4", "briefing"], lambda ws: {"input_file": _latest(ws, "s4", "scan")}, 1),
        Step("s4 content-strategy", ["s4", "content-strategy"], lambda ws: {"input_file": _latest(ws, "s4", "briefing")}, 1),
        Step("s1d article-draft", ["s1d", "article-draft"], lambda ws: {"input_file": _latest(ws, "s4", "content-strategy")}, 1),
        evaluate,
        Step("s2 homeostat run", ["s2", "homeostat", "run"], lambda ws: {}, _periodic_llm(orchestrator.HOMEOSTAT_TASKS), chain=True),
        Step("s2 newsletter run", ["s2", "newsletter", "run"], lambda ws: {}, _periodic_llm(orchestrator.NEWSLETTER_TASKS), chain=True),
    ]


def planned_calls(steps: list) -> tuple:
    return sum(s.llm_calls for s in steps), sum(s.translation_calls for s in steps)


def input_files(scope: str) -> list:
    """Every input file the run will feed to a provider, checked against the size limit up front."""
    files = [FIXTURES / "tiny_source.txt", FIXTURES / "tiny_candidate_brief.txt"]
    if scope == "full":
        files += [FIXTURES / "tiny_combined_scan.txt"]
    return files


def oversized(scope: str, limit: int = MAX_INPUT_CHARS) -> list:
    return [(p, len(p.read_text(encoding="utf-8"))) for p in input_files(scope)
            if len(p.read_text(encoding="utf-8")) > limit] + (
        [("the newsletter notes line", len(NOTES_LINE))] if scope == "full" and len(NOTES_LINE) > limit else [])


# -- running ---------------------------------------------------------------------------------

def subprocess_runner(argv: list, timeout: float) -> tuple:
    """Run a command with a time limit. A command that outlives it is killed and reported as failed."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    try:
        proc = subprocess.run(argv, cwd=REPO_ROOT, env=env, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f} s and was killed"
    return proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", errors="replace")


def _setup(ws: Workspace, ctx: Context, specs: dict, scope: str, run: Callable) -> None:
    """Make the inputs a real session would have made by hand: a book with its manuscript, a candidate
    brief and, for `full`, a finished scan and this month's newsletter notes."""
    def command(path, values):
        return commands.argv_for(ctx, specs[tuple(path)], values)

    code, out = run(command(["init"], {"book_slug": BOOK}), 120)
    if code != 0:
        raise RuntimeError(f"setup (init) failed:\n{out}")
    source = ws.book / "s1b" / "source" / "tiny_source.txt"
    shutil.copy(FIXTURES / "tiny_source.txt", source)

    code, out = run(command(["candidate", "new"], {"candidate_slug": CANDIDATE}), 120)
    if code != 0:
        raise RuntimeError(f"setup (candidate new) failed:\n{out}")
    shutil.copy(FIXTURES / "tiny_candidate_brief.txt", ws.candidate_brief)

    if scope == "full":
        scan = ws.intelligence / "s4" / "scan" / date.today().isoformat() / "combined.txt"
        scan.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / "tiny_combined_scan.txt", scan)
        (ws.intelligence / "manifest.yaml").write_text("slug: intelligence\n", encoding="utf-8")
        notes = Path(ws.config["newsletter_dir"]) / "notes" / f"{date.today().isoformat()[:7]}.txt"
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text(NOTES_LINE, encoding="utf-8")


def format_missing_files() -> bool:
    """True when `s1b format` cannot work here (its style config or template is not there). It does not
    need LibreOffice (ADR 023, Verification notes); only those two personal files."""
    styles = REPO_ROOT / "templates" / "format_styles.yaml"
    if not styles.exists():
        return True
    template = (yaml.safe_load(styles.read_text(encoding="utf-8")) or {}).get("template")
    return not template or not (REPO_ROOT / template).exists()


def judge(step: Step, code: int, output: str) -> tuple:
    """(passed, note). A book run in which only `format` failed because its template is not installed
    here is a pass: nothing about the providers failed."""
    if code == 0:
        return True, ""
    if step.chain and code == 1:
        summary = chain_summary(output.replace("\r\n", "\n").split("\n"))
        if summary and summary.failed == ["s1b.format"] and format_missing_files():
            return True, "s1b.format skipped: its template is not installed here"
    if code == 124:
        return False, output
    # The last line that names an error (a Click message, or the end of a traceback), else the last line.
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    error = next((l for l in reversed(lines) if "Error" in l or "failed" in l), lines[-1] if lines else "")
    return False, error[:200] or f"exit code {code}"


def preview(scope, steps, ws_path, provider, model, translation, with_translate, ceiling, out) -> None:
    llm, tr = planned_calls(steps)
    out(f"Live smoke test, scope: {scope}")
    out(f"  LLM:          {provider} / {model}")
    out(f"  Planned LLM calls: about {llm}" + (f" and {tr} translation request(s)" if tr else ""))
    if scope == "full":
        label = translation if with_translate else f"fake (translation skipped; --with-translate uses {translation})"
        out(f"  Translation:  {label}")
        if with_translate:
            out(f"  Expected wait: {WAITS.get(translation, 'unknown')}")
    out(f"  Cost ceiling: ${ceiling:.2f} (PIPELINE_LIVE_MAX_USD); the run stops as soon as it is exceeded")
    out(f"  Workspace:    {ws_path} (temporary data folders; your own books, candidates and intelligence are untouched)")
    out("  Commands:     " + "; ".join(s.name for s in steps))


# -- the entry point ------------------------------------------------------------------------------

def main(argv=None, *, runner=subprocess_runner, confirm=input, environ=None, config_path=None,
         pipeline_script=None, load_env=True, root=None, out=print, size_limit=MAX_INPUT_CHARS) -> int:
    parser = argparse.ArgumentParser(description="Live smoke test with real providers (ADR 023, T8.2).")
    parser.add_argument("--scope", choices=["core", "full"], required=True)
    parser.add_argument("--with-translate", action="store_true", help="use the configured translation provider in `full`")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--clean", action="store_true", help="delete the workspace at the end")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and exit without calling anything")
    args = parser.parse_args(argv)

    environ = os.environ if environ is None else environ
    config_path = Path(config_path or REPO_ROOT / "config.yaml")
    pipeline_script = Path(pipeline_script or REPO_ROOT / "pipeline.py")
    os.chdir(REPO_ROOT)   # manifests, prompts and templates are repository-relative
    if load_env:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")

    if not config_path.exists():
        out(f"Refusing to start: {config_path} does not exist.")
        return 2
    real_config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    provider = (real_config.get("llm") or {}).get("provider")
    model = (real_config.get("llm") or {}).get("model")
    translation = (real_config.get("translation") or {}).get("provider")
    if not provider or not model:
        out("Refusing to start: llm.provider and llm.model must be set in the config.")
        return 2

    too_big = oversized(args.scope, size_limit)
    if too_big:
        for what, size in too_big:
            out(f"Refusing to start: {what} has {size:,} characters; the limit is {size_limit:,}.")
        return 2

    steps = plan(args.scope, args.with_translate)
    ceiling = float(environ.get("PIPELINE_LIVE_MAX_USD", DEFAULT_MAX_USD))
    llm_calls, _ = planned_calls(steps)

    if args.dry_run:
        preview(args.scope, steps, "(none created in a dry run)", provider, model, translation, args.with_translate, ceiling, out)
        return 0

    ws = Workspace(real_config, args.with_translate)
    ctx = Context(repo_root=REPO_ROOT, config_path=ws.config_path, config=ws.config, pipeline_script=pipeline_script)
    # Missing credentials are caught here, with the name of the variable, instead of as a failed first
    # command. Translation credentials matter only when the real translation provider will be used.
    needed_areas = {"LLM provider"} | ({"Translation provider"} if args.scope == "full" and args.with_translate else set())
    problems = [c for c in health.check_providers(ctx, environ) if c.status == health.ERROR and c.area in needed_areas]
    if problems:
        for c in problems:
            out(f"Refusing to start: {c.area}: {c.label}: {c.detail}")
        shutil.rmtree(ws.base, ignore_errors=True)
        return 2

    preview(args.scope, steps, ws.base, provider, model, translation, args.with_translate, ceiling, out)
    quota = DAILY_QUOTAS.get(provider)
    if quota and llm_calls > QUOTA_WARN_FRACTION * quota:
        out(f"  WARNING: {llm_calls} planned calls is {llm_calls / quota:.0%} of {provider}'s known free-tier quota "
            f"({quota} requests per day).")
    if not args.yes and confirm("Run it? [y/N] ").strip().lower() not in ("y", "yes"):
        out("Declined; nothing was run.")
        shutil.rmtree(ws.base, ignore_errors=True)
        return 4

    if root is None:
        import pipeline
        root = pipeline.cli
    specs = {tuple(s.path): s for s in build_model(root)}
    results, started, exit_code = [], time.monotonic(), 0
    try:
        _setup(ws, ctx, specs, args.scope, lambda argv_, timeout: runner(argv_, timeout))
        for number, step in enumerate(steps, 1):
            begun = time.monotonic()
            command = commands.argv_for(ctx, specs[tuple(step.path)], step.values(ws))
            out(f"[{number}/{len(steps)}] {step.name}: {commands.display(command)}")
            code, output = runner(command, step.timeout)
            (ws.logs / f"{number:02d}-{step.name.replace(' ', '-')}.log").write_text(output, encoding="utf-8")
            passed, note = judge(step, code, output)
            cost = ws.total_cost()
            results.append(Result(step.name, passed, time.monotonic() - begun, note, cost))
            out(f"      {'PASS' if passed else 'FAIL'}  {results[-1].seconds:.1f} s, cost so far ${cost:.6f}" + (f"  ({note})" if note else ""))
            if cost > ceiling:
                out(f"Stopping: ${cost:.4f} recorded in the ledgers exceeds the ${ceiling:.2f} ceiling.")
                exit_code = 3
                break
            if not passed:
                exit_code = exit_code or 1
    except KeyboardInterrupt:
        out("Interrupted.")
        exit_code = 130
    except (RuntimeError, OSError) as exc:
        out(f"Setup failed: {exc}")
        exit_code = 1

    ran = results
    done_steps = steps[:len(ran)]
    llm_done, tr_done = planned_calls(done_steps)
    out("")
    out(f"Result: {sum(r.passed for r in ran)} of {len(steps)} commands passed"
        + (f", {len(steps) - len(ran)} not run" if len(ran) < len(steps) else ""))
    out(f"  LLM calls: about {llm_done} (planned for the commands that ran)" + (f", translation requests: {tr_done}" if tr_done else ""))
    out(f"  Duration:  {time.monotonic() - started:.1f} s")
    out(f"  Cost:      ${ws.total_cost():.6f} recorded in the ledgers (ceiling ${ceiling:.2f})")
    out(f"  Workspace: {ws.base}" + ("" if not args.clean else "  (deleted: --clean)"))
    if args.clean:
        shutil.rmtree(ws.base, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
