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

# A fourth variant, found during ADR 015's real Groq validation testing (not
# previously observed against Mistral): the model wraps a bare Roman
# numeral in brackets, mid-sentence, as if it needed to be flagged —
# "CAPÍTULO [I]" instead of "CAPÍTULO I", "el siglo [VI]" instead of "el
# siglo VI" — both confirmed in one real `ortho` output. Same underlying
# tendency as _BRACKETED_HEADING_RE above (something that reads as
# structurally significant gets wrapped in brackets unprompted), just
# landing on a numeral instead of a whole heading line, so the same
# unconditional-strip fix applies. Matches uppercase-only, since this
# project's own markup (`[i]`, `[sc]`, `[FN: ...]`) is always lowercase or
# has a colon — a case-sensitive, letters-only pattern can't collide with
# it. Validated against the strict Roman-numeral form (not just "any
# combination of I/V/X/L/C/D/M") to keep the false-positive surface small;
# a bracketed acronym that happens to also parse as a valid numeral (e.g.
# "[CD]") is the accepted residual risk, same class of tradeoff the other
# three normalizers already accept.
_BRACKETED_ROMAN_NUMERAL_RE = re.compile(r"\[([IVXLCDM]+)\]")
_VALID_ROMAN_NUMERAL_RE = re.compile(r"^M{0,4}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")


def _normalize_headings(text: str) -> str:
    while _DOUBLED_HEADING_RE.search(text):
        text = _DOUBLED_HEADING_RE.sub("", text)
    text = _BRACKETED_HEADING_RE.sub(r"\1 \2", text)
    return text


def _strip_bracketed_roman_numerals(text: str) -> str:
    def _unwrap(match: re.Match) -> str:
        numeral = match.group(1)
        return numeral if _VALID_ROMAN_NUMERAL_RE.match(numeral) else match.group(0)
    return _BRACKETED_ROMAN_NUMERAL_RE.sub(_unwrap, text)


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


def _estimate_tokens(system_prompt: str, text: str) -> int:
    """Rough token estimate for the preflight below: ~3 characters per
    token, deliberately conservative for Spanish prose (the language most of
    this project's prompts and manuscripts are written in) rather than tuned
    to English's typical ~4. An early-warning heuristic only — the
    provider's own 429 response, not this estimate, is authoritative. See
    docs/adr/015-groq-llm-provider.md, Decision 3.
    """
    return (len(system_prompt) + len(text)) // 3


def _check_request_size(system_prompt: str, text: str, max_request_tokens: int, label: str) -> None:
    """Optional preflight, gated by `llm.max_request_tokens` in config.yaml
    (unset = disabled, preserving today's behavior exactly). Catches a
    request too large for a provider's per-request/per-minute token ceiling
    (e.g. Groq's free tier) before any API call is made, at zero token cost
    — the same shape and reasoning as the `single_chunk` guard above. See
    docs/adr/015-groq-llm-provider.md, Decision 3.
    """
    if not max_request_tokens:
        return
    est = _estimate_tokens(system_prompt, text)
    if est > max_request_tokens:
        raise click.ClickException(
            f"{label}: ~{est} estimated tokens ({len(text)} chars of input "
            f"plus the system prompt) exceeds llm.max_request_tokens="
            f"{max_request_tokens}. Lower max_chars for this task, or raise "
            f"the limit if your account's tokens-per-minute allowance is "
            f"higher — see docs/adr/015-groq-llm-provider.md."
        )


def run(input_file: Path, root: Path, system: str, output_name: str, config: dict,
        *, prompt: str, manifest_key: str = None, max_chars: int = 8000,
        temperature: float = None, metadata_config: str = None,
        metadata_footer: str = None, single_chunk: bool = False,
        reduce_prompt: str = None) -> Path:
    output_dir = paths.stage_output_dir(input_file, root, system, output_name)
    output_file = output_dir / input_file.name

    system_prompt = paths.load_prompt(prompt)
    # Loaded up front, alongside system_prompt, so a missing/misconfigured
    # reduce prompt file fails before any LLM calls, not partway through.
    reduce_system_prompt = paths.load_prompt(reduce_prompt) if reduce_prompt else None
    metadata = metadata_blocks.load(metadata_config) if metadata_config else None
    llm = get_llm_provider(config)
    max_request_tokens = config["llm"].get("max_request_tokens")

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

    # Preflight (ADR 015, Decision 3): checks the single largest chunk — if
    # that one fits under a configured per-request token ceiling, every
    # smaller chunk does too, so nothing is gained checking each one.
    if chunks:
        largest = max(chunks, key=len)
        _check_request_size(system_prompt, largest, max_request_tokens, prompt)

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
        # Checked here too, not just over the chunks above: the reduce call
        # sends the *concatenation* of every map-step draft, which can be
        # larger than any individual chunk and whose size isn't knowable
        # until now. The map tokens are already spent by this point, but the
        # reduce call's aren't yet.
        _check_request_size(reduce_system_prompt, labeled_drafts, max_request_tokens, reduce_prompt)
        reduced = llm.complete(reduce_system_prompt, labeled_drafts, temperature=temperature)
        parts = [_strip_code_fence(reduced)]

    output_text = _strip_bracketed_roman_numerals(_normalize_headings("\n\n".join(parts)))
    if metadata:
        output_text = metadata_blocks.substitute(output_text, metadata)
    if metadata_footer:
        output_text = metadata_blocks.append_footer(output_text, metadata, metadata_footer)
    # Created here, not up front: a run that fails or is interrupted before
    # this point (e.g. a provider exhausting its 429 retries) must not leave
    # behind an empty dated directory that looks like a completed-but-empty
    # run — see the s4 briefing 429 investigation.
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file.write_text(output_text, encoding="utf-8")

    key = manifest_key or output_name
    existing = manifest.load(root)
    source_kwargs = {} if "source" in existing else {"source": str(input_file.relative_to(root))}
    manifest.update(
        root,
        **{key: str(output_file.relative_to(root))},
        llm_provider=config["llm"]["provider"],
        # No fallback needed: get_llm_provider() above already raised if
        # llm.model was unset, so it's guaranteed present by the time this
        # runs — see docs/adr/015-groq-llm-provider.md.
        llm_model=config["llm"]["model"],
        **source_kwargs,
    )

    click.echo(f"Saved: {output_file}")
    return output_file, {"usage": llm.usage}
