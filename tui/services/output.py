"""Reading what a command printed (ADR 023). Plain Python, no Textual.

`s2 run` and the periodic chains end with a block like

    Done: 5, Failed: 1
      [x] s1b.cleanup
      [!] s1b.format
    Book complete - nothing left to run.

which the run modal turns into a sentence. This module parses that block.
"""
import re
from dataclasses import dataclass, field

_COUNTS = re.compile(r"^Done: (\d+), Failed: (\d+)\s*$")
_ITEM = re.compile(r"^\s+\[(x|!)\] (\S+)\s*$")


@dataclass
class ChainSummary:
    done: list = field(default_factory=list)
    failed: list = field(default_factory=list)

    def sentence(self) -> str:
        text = f"{len(self.done)} task{'s' if len(self.done) != 1 else ''} done, {len(self.failed)} failed"
        return f"{text}: {', '.join(self.failed)}." if self.failed else f"{text}."


def chain_summary(lines: list):
    """The `Done: N, Failed: M` block of a chain run, or None if the output has none
    (the command stopped before reaching it, or is not a chain)."""
    start = next((i for i in range(len(lines) - 1, -1, -1) if _COUNTS.match(lines[i])), None)
    if start is None:
        return None
    summary = ChainSummary()
    for line in lines[start + 1:]:
        item = _ITEM.match(line)
        if not item:
            break
        (summary.done if item.group(1) == "x" else summary.failed).append(item.group(2))
    return summary
