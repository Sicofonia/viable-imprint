import re
from pathlib import Path

import click

from lib.chunker import chunk_by_paragraphs
from lib import manifest, metadata_blocks, paths
from providers import get_llm_provider

# Occasionally observed across markdown-heading prompts: the model prefixes a
# correct heading with a spurious extra heading marker of its own, e.g.
# "# ## Biografía del autor" or "### ### La puerta norte..." instead of just
# "## Biografía del autor" / "### La puerta norte...". Prompt wording alone
# didn't eliminate this reliably across repeated runs, so it's normalized
# here instead — safe to apply unconditionally, since no legitimate line
# ever starts with two consecutive heading markers.
_DOUBLED_HEADING_RE = re.compile(r"^#{1,6} (?=#{1,6} )", re.MULTILINE)

# Same story as above, different defect: despite an explicit "don't wrap in
# a code block" rule, a chunk's response occasionally comes back as one
# big ```-fenced block anyway. Stripped per-chunk (not just on the final
# joined text) so a fence around one chunk of a multi-chunk response still
# gets caught.
_CODE_FENCE_RE = re.compile(r"^```[^\n]*\n(.*)\n```$", re.DOTALL)

# A third variant: sometimes the model reproduces a heading's *position*
# faithfully but wraps the whole thing in a single bracket pair anyway
# ("## [¿Qué ha ocurrido en este período?]"), even when told explicitly
# never to put brackets in a heading and even after removing every bracket
# from the template's own heading lines. Three rounds of prompt wording
# (docs/adr/003-system-4-strategic-intelligence.md's `briefing` task) didn't
# eliminate it, so — same call as the other two normalizers above — it's
# fixed in code: no legitimate heading is ever entirely wrapped in one
# matching bracket pair, so stripping the brackets is safe unconditionally.
# Heading *wording* still varies run to run; only the visible bracket
# artifact is being fixed here.
_BRACKETED_HEADING_RE = re.compile(r"^(#{1,6}) \[(.+)\]$", re.MULTILINE)


def _normalize_headings(text: str) -> str:
    while _DOUBLED_HEADING_RE.search(text):
        text = _DOUBLED_HEADING_RE.sub("", text)
    text = _BRACKETED_HEADING_RE.sub(r"\1 \2", text)
    return text


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    match = _CODE_FENCE_RE.match(text)
    return match.group(1).strip() if match else text


def _wrap_draft(index: int, total: int, text: str) -> str:
    """Delimit one map-step draft for the reduce call. XML-style tags, not a
    Markdown-heading-like "=== Borrador N ===" marker — `feed_scan._wrap_source`
    already found that the latter visually primes a model to paraphrase its
    own fixed output headings into the same descriptive style instead of
    reproducing them literally (ADR 003 point 10). Several reduce prompts
    (press-dossier, one-pager, story-map) must reproduce fixed headings, so
    the same risk applies here and the same fix is reused.
    """
    return f'<borrador numero="{index}" total="{total}">\n{text}\n</borrador>'


def run(input_file: Path, root: Path, system: str, output_name: str, config: dict,
        *, prompt: str, manifest_key: str = None, max_chars: int = 8000,
        temperature: float = None, metadata_config: str = None,
        metadata_footer: str = None, single_chunk: bool = False,
        reduce_prompt: str = None) -> Path:
    output_dir = paths.stage_output_dir(input_file, root, system, output_name)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / input_file.name

    system_prompt = paths.load_prompt(prompt)
    # Loaded up front, alongside system_prompt, so a missing/misconfigured
    # reduce prompt file fails before any LLM calls, not partway through.
    reduce_system_prompt = paths.load_prompt(reduce_prompt) if reduce_prompt else None
    metadata = metadata_blocks.load(metadata_config) if metadata_config else None
    llm = get_llm_provider(config)

    raw_text = input_file.read_text(encoding="utf-8")
    chunks = chunk_by_paragraphs(raw_text, max_chars=max_chars)
    total = len(chunks)

    # See docs/adr/014-system1d-brief-chunk-overflow-guard.md: a task marked
    # single_chunk must synthesize its whole input as one coherent document —
    # if it silently chunked instead, each chunk would produce its own
    # complete, mutually inconsistent document, concatenated together with no
    # error. A task with a reduce_prompt configured can still reconcile a
    # multi-chunk result into one document below; one without it can't, so
    # that case still fails loud, before any LLM call, at zero token cost.
    if single_chunk and total > 1 and not reduce_prompt:
        raise click.ClickException(
            f"{prompt} requires a single call ({len(raw_text)} chars > "
            f"max_chars={max_chars}), but chunking produced {total} chunks. "
            f"This task cannot synthesize a coherent document from partial "
            f"input — see docs/adr/014-system1d-brief-chunk-overflow-guard.md."
        )

    parts = []
    for i, chunk in enumerate(chunks, 1):
        click.echo(f"  {output_name} chunk {i}/{total}...")
        parts.append(_strip_code_fence(llm.complete(system_prompt, chunk, temperature=temperature)))

    # Reduce pass (ADR 014, Decision 2): only when chunking actually happened
    # and this task has a reduce prompt configured. Reconciles the N
    # independent per-chunk drafts above into one final document — skipped
    # entirely (no added cost) when there was only ever one chunk.
    if total > 1 and reduce_prompt:
        click.echo(f"  {output_name} reduce: reconciling {total} drafts into one...")
        labeled_drafts = "\n\n".join(_wrap_draft(i, total, part) for i, part in enumerate(parts, 1))
        reduced = llm.complete(reduce_system_prompt, labeled_drafts, temperature=temperature)
        parts = [_strip_code_fence(reduced)]

    output_text = _normalize_headings("\n\n".join(parts))
    if metadata:
        output_text = metadata_blocks.substitute(output_text, metadata)
    if metadata_footer:
        output_text = metadata_blocks.append_footer(output_text, metadata, metadata_footer)
    output_file.write_text(output_text, encoding="utf-8")

    key = manifest_key or output_name
    existing = manifest.load(root)
    source_kwargs = {} if "source" in existing else {"source": str(input_file.relative_to(root))}
    manifest.update(
        root,
        **{key: str(output_file.relative_to(root))},
        llm_provider=config["llm"]["provider"],
        llm_model=config["llm"].get("model", "mistral-medium-latest"),
        **source_kwargs,
    )

    click.echo(f"Saved: {output_file}")
    return output_file, {"usage": llm.usage}
