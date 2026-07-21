"""System 5's policy-drift tripwire (ADR 013) — a stored hash of the exact
`docs/vsm.md` subsection `prompts/s5/policy_evaluation_task.txt` is derived
from, checked by `pipeline.py s5 check-policy-sync`. Not the whole
"## System 5" section (see ADR 013's Context — checked against the real
prompt directly: it draws only from thematic scope in/out and non-negotiable
values, not Description/Responsibilities/Practical Expressions/Qualitative
Metrics).
"""
import hashlib
import re
from pathlib import Path

_SECTION_START = "### Editorial Policy (Constitutive Criteria)"
# [ \t]* (not \s*) before $ — under re.MULTILINE, \s matches newlines too, so
# a trailing \s* here would greedily consume the blank line after this one,
# silently eating it on every sub() call. Found by testing update_stored_hash()
# against a real file and diffing the result, not assumed correct.
_HASH_LINE = re.compile(r"^# vsm-sync-hash:[ \t]*([0-9a-f]+)[ \t]*$", re.MULTILINE)


def extract_policy_section(vsm_path: str = "docs/vsm.md") -> str:
    """From "### Editorial Policy (Constitutive Criteria)" up to the next
    "#"-prefixed heading line, whichever comes first. Raises if the heading
    isn't found at all — vsm.md restructured more than expected; fail loud
    rather than silently hash the wrong thing or an empty string.
    """
    lines = Path(vsm_path).read_text(encoding="utf-8").splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == _SECTION_START), None)
    if start is None:
        raise ValueError(f"Could not find {_SECTION_START!r} in {vsm_path} — has vsm.md been restructured?")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("#")), len(lines))
    return "\n".join(lines[start:end]).strip()


def current_hash(vsm_path: str = "docs/vsm.md") -> str:
    return hashlib.sha256(extract_policy_section(vsm_path).encode("utf-8")).hexdigest()[:12]


def stored_hash(prompt_path: str = "prompts/s5/policy_evaluation_task.txt") -> str:
    text = Path(prompt_path).read_text(encoding="utf-8")
    match = _HASH_LINE.search(text)
    return match.group(1) if match else None


def update_stored_hash(new_hash: str, prompt_path: str = "prompts/s5/policy_evaluation_task.txt") -> None:
    path = Path(prompt_path)
    text = path.read_text(encoding="utf-8")
    replacement = f"# vsm-sync-hash: {new_hash}"
    if _HASH_LINE.search(text):
        text = _HASH_LINE.sub(replacement, text)
    else:
        text = f"{replacement}\n{text}"
    path.write_text(text, encoding="utf-8")
