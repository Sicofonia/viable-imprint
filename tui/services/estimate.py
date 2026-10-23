"""What a run is about to ask of a provider, before it runs (ADR 023, Decision 5).

For an action whose engine calls an LLM or translation provider, or a chain that
contains one: the provider and model, the input size in characters, an
approximate number of calls and an estimated cost from the configured `pricing:`
block (or "no pricing configured"). On free tiers the daily request quota is the
scarce resource, so the call count is the figure that matters most.

The numbers reuse the engines' own rules (`_resolve_default_max_chars`, the
task's `max_chars`, the provider's `max_chars_per_request`) so the estimate and
the run chunk the input the same way. They are approximations: the output length
is assumed equal to the input length, and a token is four characters.

Plain Python, no Textual.
"""
import importlib
import inspect
from dataclasses import dataclass, field
from pathlib import Path

from engines import llm_text
from lib import formatting, metrics, orchestrator, paths, task_loader
from lib.chunker import chunk_by_paragraphs
from providers.translation.base import TranslationProvider
from tui.services.context import Context

_CHARS_PER_TOKEN = 4
_DEFAULT_TRANSLATION_CHUNK = 50000

# Engines that call a provider, and which config block configures it.
PROVIDER_ENGINES = {"llm_text": "llm", "translation": "translation"}


@dataclass
class Estimate:
    kind: str                       # "llm" | "translation" | "chain"
    provider: str = None
    model: str = None
    input_chars: int = None         # None when the input does not exist yet
    calls: int = 0
    cost_usd: float = None          # None: no pricing configured
    notes: list = field(default_factory=list)
    items: list = field(default_factory=list)   # a chain's per-task estimates
    label: str = ""                # what this estimate is for, e.g. "s1b cleanup"

    @property
    def pricing_configured(self) -> bool:
        return self.cost_usd is not None

    def lines(self) -> list:
        out = []
        if self.kind == "chain":
            for item in self.items:
                out.append(f"- {item.label}: {item.calls} call{'s' if item.calls != 1 else ''}"
                           + (f", {item.input_chars:,} characters" if item.input_chars is not None else ""))
        provider = self.provider if not self.model else f"{self.provider} / {self.model}"
        out.append(f"Provider: {provider}")
        if self.kind != "chain":
            out.append(f"Input: {self.input_chars:,} characters" if self.input_chars is not None
                       else "Input: size not known yet")
        out.append(f"Provider calls: about {self.calls}")
        out.append(f"Estimated cost: {formatting.format_cost(self.cost_usd)}" if self.pricing_configured
                   else "Estimated cost: no pricing configured")
        return out + self.notes


def _task(system: str, name: str):
    return next((t for t in task_loader.load_system_tasks(system) if t["name"] == name), None)


def provider_engine(system: str, name: str):
    """The task's engine if it calls a provider (`llm_text`, `translation`), else None."""
    task = _task(system, name)
    return task["engine"] if task and task["engine"] in PROVIDER_ENGINES else None


def _translation_chunk_limit(provider_name: str) -> int:
    try:
        module = importlib.import_module(f"providers.translation.{provider_name}")
    except ImportError:
        return _DEFAULT_TRANSLATION_CHUNK
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if issubclass(cls, TranslationProvider) and cls is not TranslationProvider and cls.__module__ == module.__name__:
            return getattr(cls, "max_chars_per_request", _DEFAULT_TRANSLATION_CHUNK)
    return _DEFAULT_TRANSLATION_CHUNK


def _read(input_file) -> str:
    if input_file is None:
        return None
    path = Path(input_file)
    return path.read_text(encoding="utf-8") if path.is_file() else None


def estimate_task(ctx: Context, system: str, name: str, input_file=None, *, document: bool = False):
    """Estimate for one manifest task, or None if it makes no provider call."""
    task = _task(system, name)
    if task is None or task["engine"] not in PROVIDER_ENGINES:
        return None
    config = ctx.config
    text = _read(input_file)
    notes = []

    if task["engine"] == "llm_text":
        llm = config.get("llm", {})
        estimate = Estimate("llm", provider=llm.get("provider"), model=llm.get("model"),
                            input_chars=len(text) if text is not None else None, label=f"{system} {name}")
        if text is None:
            estimate.calls = 1
            return estimate
        max_chars = task.get("max_chars") or llm_text._resolve_default_max_chars(config)
        chunks = chunk_by_paragraphs(text, max_chars=max_chars)
        reduce = len(chunks) > 1 and task.get("reduce_prompt")
        if task.get("single_chunk") and len(chunks) > 1 and not reduce:
            estimate.calls = 0
            estimate.notes.append(f"The input ({len(text):,} characters) exceeds this task's {max_chars:,}-character "
                                  "limit and the task cannot be split: it will stop before calling the provider.")
            return estimate
        estimate.calls = len(chunks) + (1 if reduce else 0)
        try:
            system_prompt_chars = len(paths.load_prompt(task["prompt"]))
        except Exception:  # a missing prompt is reported by the run itself; the estimate just goes without it
            system_prompt_chars = 0
            estimate.notes.append(f"Prompt file {task['prompt']} could not be read; the cost excludes it.")
        usage = {
            "prompt_tokens": (system_prompt_chars * len(chunks) + len(text)) // _CHARS_PER_TOKEN,
            "completion_tokens": len(text) // _CHARS_PER_TOKEN,
        }
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        estimate.cost_usd = metrics.enrich({"usage": usage, "model": llm.get("model")}, config).get("cost_usd")
        return estimate

    translation = config.get("translation", {})
    provider_name = translation.get("provider")
    estimate = Estimate("translation", provider=provider_name, input_chars=len(text) if text is not None else None,
                        label=f"{system} {name}")
    if provider_name == "etranslation":
        estimate.notes.append("eTranslation is asynchronous: expect a wait of minutes, up to two hours. "
                              "If the run is interrupted, running it again resumes the request already in flight.")
    if text is None:
        estimate.calls = 1
        return estimate
    estimate.calls = 1 if document else len(chunk_by_paragraphs(text, max_chars=_translation_chunk_limit(provider_name)))
    estimate.cost_usd = metrics.enrich({"usage": {"characters": len(text)}}, config).get("cost_usd")
    return estimate


def _combine(items: list, notes: list):
    if not items:
        return None
    first = items[0]
    known = [i.cost_usd for i in items if i.cost_usd is not None]
    chars = [i.input_chars for i in items if i.input_chars is not None]
    return Estimate("chain", provider=first.provider, model=first.model,
                    input_chars=sum(chars) if chars else None, calls=sum(i.calls for i in items),
                    cost_usd=sum(known) if known and len(known) == len(items) else None,
                    notes=notes, items=items)


def estimate_book_run(ctx: Context, slug: str, *, only: str = None, step: bool = False):
    """The provider-calling tasks that are ready right now for `s2 run <slug>`."""
    root = ctx.data_dir("books_dir") / slug
    ready = orchestrator.ready_tasks(root, only=only)
    if step:
        ready = ready[:1]
    items = []
    for node, input_path in ready:
        item = estimate_task(ctx, node["system"], node["task"]["name"], input_path)
        if item is not None:
            items.append(item)
    notes = [] if step else ["More tasks become ready as the run proceeds; this covers only what is ready now."]
    return _combine(items, notes)


def estimate_periodic(ctx: Context, tasks: list, *, step: bool = False):
    """A periodic chain (`HOMEOSTAT_TASKS`, `NEWSLETTER_TASKS`). The inputs of its LLM
    stage are produced by an earlier stage, so their size is not known in advance."""
    items = []
    for system, name in (tasks[:1] if step else tasks):
        item = estimate_task(ctx, system, name)
        if item is not None:
            items.append(item)
    return _combine(items, ["The LLM stage reads the output of the stage before it, so its size is not known yet."])


def estimate_command(ctx: Context, path: list, values: dict):
    """Estimate for any command the explorer or a screen is about to run, or None
    when it makes no provider call."""
    if path == ["s2", "run"]:
        return estimate_book_run(ctx, values.get("book_slug", ""), only=values.get("only"), step=bool(values.get("step")))
    if path == ["s2", "homeostat", "run"]:
        return estimate_periodic(ctx, orchestrator.HOMEOSTAT_TASKS, step=bool(values.get("step")))
    if path == ["s2", "newsletter", "run"]:
        return estimate_periodic(ctx, orchestrator.NEWSLETTER_TASKS, step=bool(values.get("step")))
    if len(path) == 2:
        return estimate_task(ctx, path[0], path[1], values.get("input_file"), document=bool(values.get("document")))
    return None
