"""Environment health (ADR 023, T8.1): what would make a run fail, found before it does.
Read-only. Plain Python, no Textual.

Secrets are never shown: an environment variable is reported as set or missing and its
value is never read into any text this module produces.
"""
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from lib import metrics
from tui.services import state
from tui.services.context import DATA_DIRS, ConfigError, Context

OK, WARNING, ERROR, INFO = "ok", "warning", "error", "info"

# Which environment variables each provider needs (providers/__init__.py reads exactly these;
# a test keeps the two in step).
LLM_ENV = {"z-ai": ["ZAI_API_KEY"], "mistral": ["MISTRAL_API_KEY"],
           "google-aistudio": ["GOOGLE_AISTUDIO_API_KEY"], "fake": []}
TRANSLATION_ENV = {"etranslation": ["ETRANSLATION_APP_NAME", "ETRANSLATION_PASSWORD", "ETRANSLATION_RECEIVER_SECRET"],
                   "deepl": ["DEEPL_API_KEY"], "fake": []}

# manifest keys that name a file the task needs
FILE_KEYS = ("prompt", "reduce_prompt", "metadata_config", "style_config", "sources_config")
PLACEHOLDER_MARKER = "your-project"

LIBREOFFICE_PATHS = (r"C:\Program Files\LibreOffice\program\soffice.exe",
                     r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
                     "/Applications/LibreOffice.app/Contents/MacOS/soffice")


@dataclass
class Check:
    area: str
    label: str
    status: str            # ok | warning | error | info
    detail: str = ""
    fix: tuple = None      # ("file", Path) to open, or ("screen", name) to go to

    @property
    def mark(self) -> str:
        return {OK: "OK", WARNING: "WARN", ERROR: "ERROR", INFO: "info"}[self.status]


def _fix_file(path: Path):
    return ("file", path) if path is not None and Path(path).exists() else None


def _example_for(path: Path, repo: Path):
    """The shipped example a missing personal file is copied from, if there is one."""
    try:
        rel = path.relative_to(repo)
    except ValueError:
        return None
    if len(rel.parts) >= 3 and rel.parts[0] == "prompts":
        candidate = repo / "prompts" / "examples" / rel.parts[1] / rel.name
    elif path.suffix == ".yaml":
        candidate = path.with_name(f"{path.stem}.example.yaml")
    else:
        return None
    return candidate if candidate.exists() else None


# -- the checks ----------------------------------------------------------------------------

def check_config(ctx: Context) -> list:
    try:
        ctx.reload_config()
    except ConfigError as exc:
        return [Check("Config", "Config file", ERROR, str(exc), _fix_file(ctx.config_path))]
    return [Check("Config", "Config file", OK, f"{ctx.config_path} was found and parsed.", _fix_file(ctx.config_path))]


def _provider_checks(ctx: Context, area: str, section: str, table: dict, environ) -> list:
    config = ctx.config.get(section) or {}
    checks = []
    provider = config.get("provider")
    env_file = ctx.repo_root / ".env"
    fix_env = _fix_file(env_file) or _fix_file(ctx.repo_root / ".env.example")
    if not provider:
        return [Check(area, f"{section}.provider", ERROR, f"{section}.provider is not set in the config.", _fix_file(ctx.config_path))]
    if provider not in table:
        return [Check(area, f"{section}.provider", ERROR,
                      f"Unknown provider {provider!r}. Supported: {', '.join(sorted(table))}.", _fix_file(ctx.config_path))]
    checks.append(Check(area, "Provider", WARNING if provider == "fake" else OK,
                        f"{provider}" + (" (the test provider: not for real work)" if provider == "fake" else "")))
    if section == "llm":
        model = config.get("model")
        checks.append(Check(area, "Model", OK if model else ERROR, model or "llm.model is not set; there is no default across providers.",
                            None if model else _fix_file(ctx.config_path)))
    for name in table[provider]:
        present = bool(environ.get(name))
        checks.append(Check(area, name, OK if present else ERROR, "set" if present else "missing (add it to .env)",
                            None if present else fix_env))
    return checks


def check_providers(ctx: Context, environ=None) -> list:
    environ = os.environ if environ is None else environ
    checks = _provider_checks(ctx, "LLM provider", "llm", LLM_ENV, environ)
    checks += _provider_checks(ctx, "Translation provider", "translation", TRANSLATION_ENV, environ)

    translation = ctx.config.get("translation") or {}
    if translation.get("provider") == "etranslation":
        url = ((translation.get("etranslation") or {}).get("receiver_url") or "")
        if not url or PLACEHOLDER_MARKER in url:
            checks.append(Check("Translation provider", "receiver_url", ERROR,
                                "translation.etranslation.receiver_url is not set, or is still the example placeholder.",
                                _fix_file(ctx.config_path)))
        elif not url.startswith("https://") or "?" in url:
            checks.append(Check("Translation provider", "receiver_url", ERROR,
                                "receiver_url must be an https URL with no query string.", _fix_file(ctx.config_path)))
        else:
            checks.append(Check("Translation provider", "receiver_url", OK, "set"))
    return checks


def check_pricing(ctx: Context) -> list:
    checks = []
    for label, usage, provider_key in (("LLM pricing", {"total_tokens": 1, "prompt_tokens": 1, "completion_tokens": 0}, "llm"),
                                       ("Translation pricing", {"characters": 1}, "translation")):
        provider = (ctx.config.get(provider_key) or {}).get("provider")
        try:
            cost = metrics.enrich({"usage": usage}, ctx.config).get("cost_usd") if provider else None
        except (KeyError, TypeError):
            cost = None
        if cost is not None:
            checks.append(Check("Pricing", f"{label} ({provider})", OK, "configured"))
        else:
            checks.append(Check("Pricing", f"{label} ({provider or 'no provider'})", INFO,
                                "no pricing configured: the dashboard shows costs as '-' and the UI cannot estimate one",
                                _fix_file(ctx.config_path)))
    return checks


def check_manifests(ctx: Context) -> list:
    """Every file named by any task in any systems/*/tasks.yaml must exist."""
    repo = ctx.repo_root
    checks = []
    for manifest_path in sorted((repo / "systems").glob("*/tasks.yaml")):
        system = manifest_path.parent.name
        try:
            tasks = (yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}).get("tasks", [])
        except yaml.YAMLError as exc:
            checks.append(Check("Task files", system, ERROR, f"{manifest_path.name} cannot be parsed: {exc}", _fix_file(manifest_path)))
            continue
        present, missing = 0, 0
        for task in tasks:
            for key in FILE_KEYS:
                if not task.get(key):
                    continue
                path = repo / task[key]
                if path.exists():
                    present += 1
                    continue
                missing += 1
                example = _example_for(path, repo)
                hint = f" Copy {example.relative_to(repo).as_posix()} to {Path(task[key]).as_posix()}." if example else ""
                checks.append(Check("Task files", f"{system} {task['name']} {key}", ERROR,
                                    f"{Path(task[key]).as_posix()} does not exist.{hint}", _fix_file(example)))
        if not missing:
            checks.append(Check("Task files", system, OK, f"all {present} file{'s' if present != 1 else ''} named in the tasks exist"))
    return checks


def libreoffice_path(which=shutil.which):
    found = which("soffice") or which("libreoffice")
    if found:
        return found
    return next((p for p in LIBREOFFICE_PATHS if Path(p).exists()), None)


def check_formatting(ctx: Context, which=shutil.which) -> list:
    checks = []
    found = libreoffice_path(which)
    checks.append(Check("Formatting", "LibreOffice", OK if found else INFO,
                        found or "not found. Only needed to export the PDF by hand before the S1C print prep; "
                                 "s1b format writes the .odt without it."))
    styles = ctx.repo_root / "templates" / "format_styles.yaml"
    if styles.exists():
        try:
            template = (yaml.safe_load(styles.read_text(encoding="utf-8")) or {}).get("template")
        except yaml.YAMLError as exc:
            checks.append(Check("Formatting", "format_styles.yaml", ERROR, f"cannot be parsed: {exc}", _fix_file(styles)))
        else:
            if not template:
                checks.append(Check("Formatting", "Book template", ERROR, "templates/format_styles.yaml names no template.", _fix_file(styles)))
            elif (ctx.repo_root / template).exists():
                checks.append(Check("Formatting", "Book template", OK, f"{template} exists"))
            else:
                checks.append(Check("Formatting", "Book template", ERROR,
                                    f"{template}, named in templates/format_styles.yaml, does not exist.", _fix_file(styles)))
    return checks


def check_policy(ctx: Context, policy_files=None) -> list:
    vsm, prompt = policy_files or (ctx.repo_root / "docs" / "vsm.md", ctx.repo_root / "prompts" / "s5" / "policy_evaluation_task.txt")
    status = state.policy_status(vsm, prompt)
    return [Check("Policy", "Policy prompt", OK if status.in_sync else WARNING, status.sentence(),
                  None if status.in_sync else ("screen", "candidates"))]


def check_data_folders(ctx: Context) -> list:
    checks = []
    for key in DATA_DIRS:
        path = ctx.data_dir(key)
        if path.is_dir():
            checks.append(Check("Data folders", key, OK, str(path)))
        else:
            checks.append(Check("Data folders", key, WARNING, f"{path} does not exist yet; it is created when first needed."))
    return checks


def run_checks(ctx: Context, *, environ=None, which=shutil.which, policy_files=None) -> list:
    checks = check_config(ctx)
    if checks[0].status == ERROR:
        return checks
    return (checks + check_providers(ctx, environ) + check_pricing(ctx) + check_manifests(ctx)
            + check_formatting(ctx, which) + check_policy(ctx, policy_files) + check_data_folders(ctx))


def summary(checks: list) -> str:
    errors = sum(c.status == ERROR for c in checks)
    warnings = sum(c.status == WARNING for c in checks)
    if not errors and not warnings:
        return "Nothing would stop a run."
    parts = [f"{errors} error{'s' if errors != 1 else ''}"] if errors else []
    parts += [f"{warnings} warning{'s' if warnings != 1 else ''}"] if warnings else []
    return ", ".join(parts) + "."
