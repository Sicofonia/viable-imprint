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


# -- `s3 sales-ingest` (T4.2) ---------------------------------------------------

_FORMAT = re.compile(r"^\s*Format: (\S+)")
_MATCHED = re.compile(r"^\s*Rows matching ISBN \S+: (\d+)")
_ADDED = re.compile(r"^\s*New entries recorded: (\d+)")
_SKIPPED = re.compile(r"^\s*Already recorded \(skipped as duplicates\): (\d+)")


@dataclass
class IngestReceipt:
    format: str = None
    matched: int = 0
    added: int = 0
    skipped: int = 0
    error: str = None       # the CLI's own error text, when it stopped before ingesting

    def sentence(self) -> str:
        if self.error:
            return self.error
        rows = f"{self.matched} row{'s' if self.matched != 1 else ''} matched this book's ISBN"
        added = f"{self.added} {'entry' if self.added == 1 else 'entries'} added"
        return f"{self.format}: {rows}, {added}, {self.skipped} skipped as duplicates."


def ingest_receipt(lines: list):
    """What `s3 sales-ingest` reported: the format, rows matched, entries added and skipped, or
    the CLI's error text (everything from its `Error:` line on). None if neither is there."""
    for i, line in enumerate(lines):
        if line.startswith("Error:"):
            return IngestReceipt(error="\n".join(lines[i:]))
    receipt = IngestReceipt()
    for line in lines:
        for pattern, attribute in ((_FORMAT, "format"), (_MATCHED, "matched"), (_ADDED, "added"), (_SKIPPED, "skipped")):
            m = pattern.match(line)
            if m:
                setattr(receipt, attribute, m.group(1) if attribute == "format" else int(m.group(1)))
    return receipt if receipt.format else None
