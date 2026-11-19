"""ADR 025 — deterministic OCR pre-pass for System 1B.

Everything about an OCR'd book that code can fix reliably is fixed here, before
`s1b cleanup` ever calls an LLM: page numbers and running headers, mangled chapter
headings, line-end hyphens, split ellipses, spaced punctuation, garbled quote
marks, a handful of certain OCR misreadings, and the layout itself (running
paragraphs in *provisional* blocks, since the OCR text carries no paragraph
indents). What is left for the LLM is judgement: which provisional blank lines
are real paragraph breaks, which ambiguous hyphens are real compounds
(`{~}` marks them), residual OCR word errors, and footnote placement.

Pure functions only: `prepass_text(text)` takes the OCR text and returns the
cleaned text plus a `Report`; `prepass_file()` adds the file I/O and
`pipeline.py s1b prepass` the CLI. Nothing here touches the ledger.

The layout this was written against (see ADR 025): every page is
`body lines, page number, blank line, RUNNING HEADER, body lines`, so blank
lines are page breaks, not paragraph breaks; chapter text has one sentence per
line while front matter is hard-wrapped; chapter openings are often mangled but
the chapter title survives, repeated, in the running headers of the pages after.
"""
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# Sentinel for a line-end hyphen the pre-pass cannot classify. The LLM removes it
# (soft hyphen) or replaces it with "-" (real compound); `s1b cleanup`'s prompt
# says so, and none may survive into the cleaned text.
HYPHEN_MARKER = "{~}"

# Provisional paragraph blocks are never allowed to grow past this without a
# break at the next sentence end (keeps chunk_by_paragraphs() able to split).
SOFT_BLOCK_CHARS = 1500

_LETTERS = "A-Za-zÀ-ɏ"
_OPEN_QUOTES = "‘“\"'"
_CLOSERS = "”’\"')]"
_SENTENCE_END_RE = re.compile(rf"[.?!…][{re.escape(_CLOSERS)}]*$")
_ABBREVIATIONS = {
    "mr", "mrs", "messrs", "dr", "st", "no", "col", "gen", "capt", "lieut", "prof",
    "vs", "viz", "cf", "ch", "vol", "lb", "cwt", "esq", "rev", "hon", "sgt", "maj",
    "mt", "ft", "oz", "p", "pp", "ibid", "etc", "e.g", "i.e", "a.d", "b.c",
}
# First element of a compound that is (almost) never a soft-hyphen fragment.
_COMPOUND_PREFIXES = {
    "self", "half", "well", "ill", "fellow", "ever", "vice", "cross", "great",
    "north", "south", "east", "west", "one", "two", "three", "four", "five", "six",
    "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
    "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
    "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred",
}
_COMPOUND_SUFFIXES = {
    "old", "year", "years", "like", "shaped", "looking", "witted", "minded",
    "hearted", "tempered", "proof", "fashioned", "going", "built", "eyed", "haired",
    "coloured", "colored", "footed", "faced", "fold", "hour", "hours", "mile",
    "miles", "bearing", "stricken",
}
# Period spellings built on a hyphen ("to-day"); the left part alone is too short
# to trust the generic rules.
_PERIOD_COMPOUNDS = {("to", "day"), ("to", "morrow"), ("to", "night")}

# Certain OCR misreadings only (a wrong guess here would be silent). Everything
# else is left to the LLM.
_OCR_FIXES = [
    ("Tf->If", re.compile(r"\bTf\b"), "If"),
    ("Tt->It", re.compile(r"\bTt\b"), "It"),
    ("T->I", re.compile(
        r"(?<![A-Za-z.])T (?=(?:am|was|have|had|will|would|think|thought|could|should|do|did|"
        r"know|must|shall|can|cannot|said|saw|told|replied|answered|asked|may|might)\b)"), "I "),
    ("|->I", re.compile(r"(?<=\s)\|(?=\s+(?:was|am|had|have|will|would|shall|could|should)\b)"), "I"),
]
_WRAPPED_MIN_LINES = 12
_WRAPPED_FULLNESS = 0.62
_SHORT_LINE_RATIO = 0.8
_GLOBAL_HEADER_INTERLEAVE = 2     # a header interleaved with >= this many other titles is the book title


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class Report:
    chars_before: int = 0
    chars_after: int = 0
    already_prepassed: bool = False
    pages: int = 0
    removed: list = field(default_factory=list)              # (kind, text)
    page_numbers_clean: int = 0
    page_number_anomalies: list = field(default_factory=list)
    pages_without_number: int = 0
    page_number_gaps: list = field(default_factory=list)     # (previous, next)
    headers_removed: Counter = field(default_factory=Counter)
    unconfirmed_headers: list = field(default_factory=list)
    chapters: list = field(default_factory=list)             # (raw numeral, assigned number, title, how)
    chapter_checks: list = field(default_factory=list)       # human-review notes
    footnotes: int = 0
    hyphens_joined: int = 0
    hyphens_kept: int = 0
    hyphens_unresolved: list = field(default_factory=list)   # "left{~}right"
    hyphens_unjoined: list = field(default_factory=list)
    ellipses_repaired: int = 0
    quote_runs_repaired: int = 0
    punctuation_spaces_fixed: int = 0
    ocr_fixes: Counter = field(default_factory=Counter)
    lead_ins_fixed: int = 0
    lead_ins: list = field(default_factory=list)
    runs: list = field(default_factory=list)                 # (label, lines, wrapped)
    blocks: int = 0
    forced_splits: int = 0
    oversized_blocks: int = 0

    def summary(self) -> str:
        if self.already_prepassed:
            return ("Input already looks pre-passed (one block per paragraph, no page apparatus): "
                    "only whitespace was normalised.")
        pct = (100.0 * (self.chars_before - self.chars_after) / self.chars_before) if self.chars_before else 0.0
        lines = [
            f"Pages:                {self.pages}",
            f"Page numbers removed: {self.page_numbers_clean} clean, {len(self.page_number_anomalies)} garbled; "
            f"{self.pages_without_number} page(s) had none",
        ]
        if self.page_number_gaps:
            lines.append(f"  Page-number sequence gaps (OCR errors or missing pages): {len(self.page_number_gaps)}")
        lines.append(f"Running headers:      {sum(self.headers_removed.values())} removed "
                     f"({', '.join(f'{k} x{v}' for k, v in self.headers_removed.most_common(5)) or 'none'})")
        if self.unconfirmed_headers:
            lines.append(f"  Caps lines kept as their own block, NOT confirmed as headers: {len(self.unconfirmed_headers)}")
        lines.append(f"Chapter headings:     {len(self.chapters)}"
                     + (f" ({len(self.chapter_checks)} need a look, see --report)" if self.chapter_checks else ""))
        lines += [
            f"Footnotes wrapped:    {self.footnotes}",
            f"Hyphens:              {self.hyphens_joined} joined, {self.hyphens_kept} kept, "
            f"{len(self.hyphens_unresolved)} marked {HYPHEN_MARKER} for the LLM"
            + (f", {len(self.hyphens_unjoined)} left unjoined" if self.hyphens_unjoined else ""),
            f"Ellipses repaired:    {self.ellipses_repaired}",
            f"Quote runs repaired:  {self.quote_runs_repaired}; spaces before punctuation fixed: {self.punctuation_spaces_fixed}",
            f"OCR misreadings:      {sum(self.ocr_fixes.values())}" + (f" ({dict(self.ocr_fixes)})" if self.ocr_fixes else ""),
            f"Blocks:               {self.blocks} ({self.forced_splits} forced splits, {self.oversized_blocks} oversized)",
            f"Text size:            {self.chars_before} -> {self.chars_after} characters ({pct:.1f}% removed)",
        ]
        return "\n".join(lines)

    def audit(self) -> str:
        """Long form for `--report`: everything that was removed or needs a human look."""
        out = [self.summary(), "", "== Removed lines =="]
        out += [f"[{kind}] {text}" for kind, text in self.removed] or ["(none)"]
        out += ["", "== Chapter headings (raw numeral line -> assigned number: title [how found]) =="]
        out += [f"{raw!r} -> {n}: {title} [{how}]" for raw, n, title, how in self.chapters] or ["(none)"]
        if self.chapter_checks:
            out += ["", "== Chapter headings that need a human look =="] + self.chapter_checks
        out += ["", f"== Hyphens left for the LLM ({HYPHEN_MARKER}) =="]
        out += self.hyphens_unresolved or ["(none)"]
        if self.hyphens_unjoined:
            out += ["", "== Hyphens NOT joined (next to a footnote) =="] + self.hyphens_unjoined
        out += ["", "== Caps lines not confirmed as running headers (kept as their own block) =="]
        out += self.unconfirmed_headers or ["(none)"]
        out += ["", "== Garbled page-end lines removed as page numbers =="]
        out += self.page_number_anomalies or ["(none)"]
        if self.page_number_gaps:
            out += ["", "== Page-number sequence gaps =="] + [f"{a} -> {b}" for a, b in self.page_number_gaps]
        out += ["", "== Chapter lead-ins sentence-cased =="]
        out += self.lead_ins or ["(none)"]
        out += ["", "== Text runs (between headings) =="]
        out += [f"{label}: {n} lines, {'hard-wrapped' if wrapped else 'sentence-per-line/other'}" for label, n, wrapped in self.runs]
        return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Items: what a page holds after structural parsing
# ---------------------------------------------------------------------------

@dataclass
class Heading:
    """A block that stands alone: chapter heading, front-matter heading, caps line."""
    lines: list
    kind: str = "section"        # "chapter" | "section" | "caps"
    raw_numeral: str = ""
    title: str = ""
    how: str = ""


@dataclass
class Footnote:
    text: str


@dataclass
class _Page:
    items: list
    gap_before: int              # blank lines before this page (0 = start of file)


# ---------------------------------------------------------------------------
# Small predicates
# ---------------------------------------------------------------------------

def _norm_key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _is_caps_line(line: str, *, min_letters: int = 3, max_len: int = 70) -> bool:
    s = line.strip()
    letters = [c for c in s if c.isalpha()]
    return (len(letters) >= min_letters and len(s) <= max_len
            and all(c.isupper() for c in letters) and re.fullmatch(r"[A-Z0-9 ,.'’\-:;&!?]+", s) is not None)


def _caps_ratio(line: str) -> float:
    letters = [c for c in line if c.isalpha()]
    return sum(1 for c in letters if c.isupper()) / len(letters) if letters else 0.0


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _is_chapter_word(token: str) -> bool:
    t = token.strip().strip(".,:").upper()
    return 5 <= len(t) <= 9 and _edit_distance(t, "CHAPTER") <= 2 and t[:1] in "CG" and t[-1:] in "RE"


_NUMERAL_LINE_RE = re.compile(r"^[IVXLCDMl1|!ilO]{1,8}[.,]?$")
_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def _to_roman(n: int) -> str:
    out = ""
    for value, sym in [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
                       (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]:
        while n >= value:
            out += sym
            n -= value
    return out


def _parse_roman(text: str):
    """Value of a clean roman numeral (or digits), else None."""
    s = text.strip().rstrip(".,").upper()
    if s.isdigit():
        return int(s)
    if not s or any(c not in _ROMAN_VALUES for c in s):
        return None
    total = 0
    for i, c in enumerate(s):
        v = _ROMAN_VALUES[c]
        total += -v if i + 1 < len(s) and _ROMAN_VALUES[s[i + 1]] > v else v
    return total if _to_roman(total) == s else None


def _is_page_number(line: str) -> bool:
    s = line.strip()
    if re.fullmatch(r"\d{1,4}", s):
        return True
    return re.fullmatch(r"[ivxlc]{1,6}", s) is not None or re.fullmatch(r"[IVXLC]{2,6}", s) is not None


def _is_number_junk(line: str) -> bool:
    """A page-end fragment that is clearly a mangled page number, not text: short,
    no 3-letter run, no sentence punctuation, no word-fragment hyphen, and
    containing a digit or a symbol, or being a lone lowercase letter
    ("Be)", "i &", "i", "I41", "- 307")."""
    s = line.strip()
    if not s or len(s) > 6 or re.search(r"[.?!”’\"']$", s) or re.search(r"[A-Za-z]-$", s) or re.search(r"[A-Za-z]{3}", s):
        return False
    if not re.search(r"[A-Za-z0-9]", s):
        return False
    return bool(re.search(r"[^A-Za-z0-9\s]|\d", s)) or re.fullmatch(r"[a-z]", s) is not None


def _is_junk_line(line: str) -> bool:
    """Stray OCR fragment on its own line. Lone dots are NOT junk (ellipsis parts)."""
    s = line.strip()
    if not s or s == ".":
        return False
    if len(s) <= 2 and not re.search(r"[A-Za-z0-9]", s) and not re.fullmatch(r"[—–\-.,;:!?’”“‘\"']+", s):
        return True
    return re.fullmatch(r"[b-hj-zB-HJ-Z]", s) is not None


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def _split_pages(lines: list) -> list:
    pages, current, gap, pending_gap = [], [], 0, 0
    for line in lines:
        if line.strip():
            if not current:
                gap = pending_gap
            current.append(line)
            pending_gap = 0
        else:
            if current:
                pages.append(_Page(current, gap))
                current = []
            pending_gap += 1
    if current:
        pages.append(_Page(current, gap))
    return pages


def _strip_page_end(page: _Page, report: Report, numbers: list) -> None:
    """Remove the page number (and mangled variants) at the end of a page."""
    items = page.items
    popped = 0
    while items and isinstance(items[-1], str) and popped < 3:
        last = items[-1]
        if popped == 0 and _is_page_number(last):
            items.pop()
            report.removed.append(("page number", last.strip()))
            report.page_numbers_clean += 1
            if re.fullmatch(r"\d{1,4}", last.strip()):
                numbers.append(int(last.strip()))
        elif _is_number_junk(last) and (popped > 0 or len(items) > 1):
            items.pop()
            report.removed.append(("garbled page number", last.strip()))
            report.page_number_anomalies.append(last.strip())
        elif popped > 0 and _is_page_number(last) and len(last.strip()) <= 2:
            items.pop()
            report.removed.append(("page number", last.strip()))
        else:
            break
        popped += 1
    if popped == 0:
        report.pages_without_number += 1


_FOOTNOTE_START_RE = re.compile(r"^\s*([1-9])\s+(?=[A-Z“‘\"(\[])")


def _extract_footnote(page: _Page, report: Report) -> None:
    """A footnote sits at the page foot, just above the (already removed) page number:
    its first line starts with a small digit and a capital. Wrap it in [FN: …] in
    place; the LLM moves it next to its reference mark."""
    items = page.items
    n = len(items)
    for j in range(max(1, n - 9), n):
        if isinstance(items[j], str) and _FOOTNOTE_START_RE.match(items[j]):
            if any(not isinstance(x, str) for x in items[j:]):
                return
            body = " ".join(x.strip() for x in items[j:])
            body = _FOOTNOTE_START_RE.sub("", body, count=1).strip()
            page.items = items[:j] + [Footnote(f"[FN: {body} [/FN]]")]
            report.footnotes += 1
            return


# ---------------------------------------------------------------------------
# Headers and chapter headings
# ---------------------------------------------------------------------------

def _find_pattern_chapter(page: _Page) -> None:
    """Replace a clean `CHAPTER / numeral / TITLE` (or `CHAPTER I. / TITLE`) run at
    any point of the page by a chapter Heading (number assigned later)."""
    items, out, i = page.items, [], 0
    while i < len(items):
        line = items[i]
        if isinstance(line, str):
            words = line.strip().split(None, 1)
            if words and _is_chapter_word(words[0]) and (len(words) == 1 or _NUMERAL_LINE_RE.match(words[1].strip())):
                raw_numeral, j = None, i + 1
                if len(words) == 2:
                    raw_numeral = words[1].strip()
                elif j < len(items) and isinstance(items[j], str) and _NUMERAL_LINE_RE.match(items[j].strip()):
                    raw_numeral, j = items[j].strip(), j + 1
                title_lines = []
                while (j < len(items) and isinstance(items[j], str) and len(title_lines) < 3
                       and _is_caps_line(items[j], min_letters=2, max_len=70)):
                    title_lines.append(items[j].strip().rstrip(".").strip())
                    j += 1
                if raw_numeral is not None and title_lines:
                    out.append(Heading([], kind="chapter", raw_numeral=raw_numeral,
                                       title=" ".join(title_lines), how="pattern"))
                    i = j
                    continue
        out.append(line)
        i += 1
    page.items = out


def _chapter_keys(header_pages: list) -> list:
    """Running-header keys that name a chapter, in order of first appearance. The
    book-title header (interleaved with several other titles, or the only kind of
    header there is, or on at least half of all header pages) and one-off headers
    are excluded — when unsure a key is NOT a chapter key, since a wrong one would
    invent a chapter. `header_pages` = [(page index, key)]."""
    by_key: dict = {}
    for pi, key in header_pages:
        by_key.setdefault(key, []).append(pi)
    if len(by_key) < 2:
        return []
    chapter_keys = []
    for key, pis in by_key.items():
        if len(pis) < 2 or len(pis) >= 0.5 * len(header_pages):
            continue
        lo, hi = min(pis), max(pis)
        others = {k for pi, k in header_pages if lo < pi < hi and k != key}
        if len(others) >= _GLOBAL_HEADER_INTERLEAVE:
            continue
        chapter_keys.append((lo, key))
    return [k for _, k in sorted(chapter_keys)]


def _match_key(raw_key: str, keys: list):
    """The known chapter key a (possibly garbled) title belongs to, or None."""
    best, best_d = None, 10 ** 6
    for key in keys:
        d = _edit_distance(raw_key, key)
        if d < best_d:
            best, best_d = key, d
    return best if best is not None and best_d <= max(2, len(best) // 4) else None


def _repair_chapter_start(page: _Page, title: str, report: Report) -> None:
    """A chapter-start page whose heading is too mangled to parse: consume the
    heading debris at the top and put a clean Heading in its place. Debris is
    everything up to the last top line that (alone or joined with its neighbours)
    matches the known title, and at least the heading-like lines at the top
    (mostly capitals, or short fragments). When the debris spells a fuller title
    than the running header (header "LIFE AMONG…", heading "HOME LIFE AMONG…"),
    the fuller one is used."""
    items = page.items
    top = []
    for it in items[:6]:
        if not isinstance(it, str):
            break
        top.append(it)
    title_key = _norm_key(title)
    fuzzy = 0
    for end in range(len(top)):
        for start in range(end + 1):
            if _edit_distance(_norm_key(" ".join(top[start:end + 1])), title_key) <= max(1, len(title_key) // 8):
                fuzzy = end + 1
    def looks_like_heading_debris(line: str) -> bool:
        text = line.strip()
        if _caps_ratio(text) >= 0.5 and len(text) <= 40:
            return True
        return len(text) < 20 and not _SENTENCE_END_RE.search(text)

    heading_like = 0
    while heading_like < min(len(top), 5) and looks_like_heading_debris(top[heading_like]):
        heading_like += 1
    consume = max(fuzzy, heading_like)
    removed, page.items = items[:consume], items[consume:]
    for line in removed:
        report.removed.append(("mangled chapter heading", line.strip()))
    for start in range(len(removed)):
        tail = removed[start:]
        joined = " ".join(l.strip() for l in tail)
        extra = len(_norm_key(joined)) - len(title_key)
        if (all(_is_caps_line(l, min_letters=2) for l in tail) and _norm_key(joined).endswith(title_key)
                and 0 < extra <= 5):
            title = joined.rstrip(".").strip()
            break
    page.items.insert(0, Heading([], kind="chapter", raw_numeral="", title=title, how="repaired from running header"))
    nxt = [x.strip() for x in page.items[1:3] if isinstance(x, str)]
    report.chapter_checks.append(f"{title}: mangled heading ({' / '.join(l.strip() for l in removed) or 'none'}) "
                                 f"replaced; text now starts: {' | '.join(nxt)[:100]}")


def _resolve_headings(pages: list, report: Report) -> set:
    """Confirm running headers, find/repair chapter headings, mark section
    headings, then remove the confirmed running headers. Returns the confirmed
    running-header keys (so a header stranded mid-page can be dropped too)."""
    for page in pages:
        _find_pattern_chapter(page)

    def first_str(page):
        first = page.items[0] if page.items else None
        return first if isinstance(first, str) and _is_caps_line(first) and not _is_chapter_word(first.split()[0]) else None

    candidates: Counter = Counter()
    raw_text: dict = {}
    section_keys = set()
    for pi, page in enumerate(pages):
        first = first_str(page)
        if first is None:
            continue
        key = _norm_key(first)
        if pi == 0 or page.gap_before >= 2:
            section_keys.add(key)
        else:
            candidates[key] += 1
            raw_text.setdefault(key, Counter())[first.strip()] += 1
    confirmed = {k for k, n in candidates.items() if n >= 2} | (set(candidates) & section_keys)

    header_pages = [(pi, _norm_key(first_str(p))) for pi, p in enumerate(pages)
                    if first_str(p) is not None and pi > 0 and p.gap_before < 2 and _norm_key(first_str(p)) in confirmed]
    chapter_keys = _chapter_keys(header_pages)
    first_header_page = {}
    for pi, key in header_pages:
        first_header_page.setdefault(key, pi)

    def title_for(key):
        return raw_text[key].most_common(1)[0][0].rstrip(".").strip()

    # Pattern headings: snap their title to a known running-header title.
    covered = set()
    for page in pages:
        for it in page.items:
            if isinstance(it, Heading) and it.kind == "chapter":
                key = _match_key(_norm_key(it.title), chapter_keys)
                if key:
                    it.title = title_for(key)
                    covered.add(key)

    # Chapters whose heading could not be parsed: recover from the running header.
    pages_with_chapter = {pi for pi, p in enumerate(pages) if any(isinstance(i, Heading) and i.kind == "chapter" for i in p.items)}
    for key in chapter_keys:
        if key in covered:
            continue
        for pi in range(first_header_page[key] - 1, max(first_header_page[key] - 4, -1), -1):
            page = pages[pi]
            has_header = first_str(page) is not None and _norm_key(first_str(page)) in confirmed and page.gap_before < 2 and pi > 0
            if has_header or pi in pages_with_chapter or not page.items:
                continue
            _repair_chapter_start(page, title_for(key), report)
            pages_with_chapter.add(pi)
            covered.add(key)
            break
        else:
            report.chapter_checks.append(f"{title_for(key)}: running header found but no chapter-start page located")

    # Section headings and confirmed running headers.
    for pi, page in enumerate(pages):
        first = first_str(page)
        if first is None:
            continue
        key = _norm_key(first)
        if pi > 0 and page.gap_before < 2 and key in confirmed:
            page.items.pop(0)
            report.removed.append(("running header", first.strip()))
            report.headers_removed[first.strip()] += 1
        elif pi == 0 or page.gap_before >= 2:
            lines_ = [first.strip()]
            if len(page.items) > 1 and isinstance(page.items[1], str) and re.fullmatch(r"(?i)by\s+\S.{0,38}", page.items[1].strip()):
                lines_.append(page.items[1].strip())
                del page.items[1]
            page.items[0] = Heading(lines_, kind="section")
    return confirmed


def _number_chapters(pages: list, report: Report) -> None:
    """Number chapter headings by order of appearance (raw numerals are the least
    reliable thing in a mangled heading) and cross-check them against the raw ones."""
    number = 0
    for page in pages:
        for it in page.items:
            if not (isinstance(it, Heading) and it.kind == "chapter"):
                continue
            parsed = _parse_roman(it.raw_numeral) if it.raw_numeral else None
            number = (parsed if parsed and number == 0 else number + 1)
            it.lines = [f"CHAPTER {_to_roman(number)}", it.title]
            report.chapters.append((it.raw_numeral, number, it.title, it.how))
            if it.raw_numeral and parsed and parsed != number:
                report.chapter_checks.append(f"{it.title}: numeral read as {it.raw_numeral!r} but it is chapter {number} by order")
            if it.raw_numeral and parsed is None and it.how == "pattern":
                report.chapter_checks.append(f"{it.title}: numeral {it.raw_numeral!r} unreadable, numbered {number} by order")


# ---------------------------------------------------------------------------
# Hyphens
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(rf"[{_LETTERS}]+(?:-[{_LETTERS}]+)*")


def _build_word_index(lines: list) -> tuple:
    solid, hyphenated = Counter(), Counter()
    previous_ended_with_hyphen = False
    for line in lines:
        text = line.rstrip()
        ends = bool(re.search(rf"[{_LETTERS}]-$", text))
        tokens = list(_TOKEN_RE.finditer(text))
        for k, m in enumerate(tokens):
            if previous_ended_with_hyphen and k == 0:
                continue                       # the second half of a split word
            if ends and k == len(tokens) - 1 and m.end() == len(text) - 1:
                continue                       # the first half of a split word
            word = m.group(0).lower()
            (hyphenated if "-" in word else solid)[word] += 1
        previous_ended_with_hyphen = ends
    return solid, hyphenated


def _decide_hyphen(left: str, right: str, solid: Counter, hyphenated: Counter) -> str:
    """Return "join", "keep" or "mark" for the pair `left-` / `right`."""
    l, r = left.lower(), right.lower()
    if not right:
        return "mark"
    if not right[0].islower() or "-" in right or "-" in left or (l, r) in _PERIOD_COMPOUNDS:
        return "keep"
    if l.split("-")[-1] in _COMPOUND_PREFIXES and len(l) > 2:
        return "keep"
    seen_solid, seen_hyphenated = solid.get(l + r, 0), hyphenated.get(f"{l}-{r}", 0)
    if seen_solid and not seen_hyphenated:
        return "join"
    if seen_hyphenated and not seen_solid:
        return "keep"
    if seen_solid and seen_hyphenated:
        return "join" if seen_solid > seen_hyphenated else ("keep" if seen_hyphenated > seen_solid else "mark")
    if r in _COMPOUND_SUFFIXES and len(l) > 2:
        return "keep"
    return "mark"


def _join_hyphens(run: list, solid: Counter, hyphenated: Counter, report: Report) -> list:
    """`run` is a list of text lines (str) and Footnote barriers. Move the first token
    of the next line onto the hyphenated line, joining, keeping or marking the hyphen."""
    lines = list(run)
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if isinstance(line, str):
            m = re.search(rf"([{_LETTERS}]+(?:-[{_LETTERS}]+)*)-\s*$", line)
            if m:
                nxt = lines[i + 1] if i + 1 < len(lines) else None
                if isinstance(nxt, str) and nxt.strip():
                    parts = nxt.strip().split(None, 1)
                    token, rest = parts[0], (parts[1] if len(parts) > 1 else "")
                    right_m = re.match(rf"[{_LETTERS}]+(?:-[{_LETTERS}]+)*", token)
                    right = right_m.group(0) if right_m else ""
                    decision = _decide_hyphen(m.group(1), right, solid, hyphenated)
                    head = line[:m.start(1)] + m.group(1)
                    if decision == "join":
                        joined = head + token
                        report.hyphens_joined += 1
                    elif decision == "keep":
                        joined = head + "-" + token
                        report.hyphens_kept += 1
                    else:
                        joined = head + HYPHEN_MARKER + token
                        report.hyphens_unresolved.append(f"{m.group(1)}{HYPHEN_MARKER}{right or token}")
                    lines[i] = joined
                    if rest:
                        lines[i + 1] = rest
                    else:
                        del lines[i + 1]
                    out.append(lines[i])
                    i += 1
                    continue
                elif nxt is not None and not isinstance(nxt, str):
                    report.hyphens_unjoined.append(line.strip()[-40:])
        out.append(line)
        i += 1
    return [_join_inline_hyphens(l, solid, hyphenated, report) if isinstance(l, str) else l for l in out]


_INLINE_HYPHEN_RE = re.compile(rf"(?<![{_LETTERS}-])([{_LETTERS}]+(?:-[{_LETTERS}]+)*)- ([{_LETTERS}]+(?:-[{_LETTERS}]+)*)")


def _join_inline_hyphens(line: str, solid: Counter, hyphenated: Counter, report: Report) -> str:
    """OCR also leaves "with- out" / "Tahta- Djdn" inside a line (a justified line
    break that kept its hyphen): same decision as for a line-end hyphen."""
    def fix(m):
        left, right = m.group(1), m.group(2)
        decision = _decide_hyphen(left, right, solid, hyphenated)
        if decision == "join":
            report.hyphens_joined += 1
            return left + right
        if decision == "keep":
            report.hyphens_kept += 1
            return f"{left}-{right}"
        report.hyphens_unresolved.append(f"{left}{HYPHEN_MARKER}{right}")
        return f"{left}{HYPHEN_MARKER}{right}"
    return _INLINE_HYPHEN_RE.sub(fix, line)


# ---------------------------------------------------------------------------
# Provisional paragraphs and text repairs
# ---------------------------------------------------------------------------

def _line_ends_sentence(line: str) -> bool:
    s = line.rstrip()
    if not _SENTENCE_END_RE.search(s):
        return False
    tokens = s.split()
    token = tokens[-1].rstrip(_CLOSERS) if tokens else ""
    if token.endswith(".") and (token.rstrip(".").lower() in _ABBREVIATIONS or re.fullmatch(r"[A-Za-z]\.", token)
                                or re.fullmatch(r"(?:[A-Za-z]\.){2,}", token)):
        return False
    return True


def _starts_opening_quote(line: str) -> bool:
    return bool(line.strip()) and line.lstrip()[:1] in _OPEN_QUOTES


def _starts_capital(line: str) -> bool:
    s = line.lstrip().lstrip("‘“\"'(")
    return bool(s) and s[0].isupper()


def _is_wrapped_run(lines: list) -> bool:
    texts = [l for l in lines if isinstance(l, str) and l.strip()]
    if len(texts) < _WRAPPED_MIN_LINES:
        return False
    widths = sorted(len(l.strip()) for l in texts)
    p90 = widths[int(len(widths) * 0.9)]
    return sum(1 for w in widths if w >= 0.85 * p90) / len(widths) >= _WRAPPED_FULLNESS


def _provisional_blocks(run: list, report: Report) -> tuple:
    """Group a run's lines into blocks (each joined into one running paragraph).
    Breaks are proposed only where a sentence ends at a line end."""
    texts = [l for l in run if isinstance(l, str) and l.strip()]
    wrapped = _is_wrapped_run(run)
    p90 = sorted(len(l.strip()) for l in texts)[int(len(texts) * 0.9)] if texts else 0
    blocks, current, size = [], [], 0
    for idx, line in enumerate(run):
        if isinstance(line, Footnote):
            current.append(line.text)
            size += len(line.text) + 1
            continue
        current.append(line.strip())
        size += len(line.strip()) + 1
        nxt = run[idx + 1] if idx + 1 < len(run) else None
        if not isinstance(nxt, str) or not _line_ends_sentence(line):
            continue
        split = False
        if _starts_opening_quote(nxt):
            split = True
        elif wrapped and len(line.strip()) < _SHORT_LINE_RATIO * p90 and _starts_capital(nxt):
            split = True
        elif size >= SOFT_BLOCK_CHARS and _starts_capital(nxt):
            split = True
            report.forced_splits += 1
        if split:
            blocks.append(" ".join(current))
            current, size = [], 0
    if current:
        blocks.append(" ".join(current))
    return blocks, wrapped


def _repair_quotes(text: str, report: Report) -> str:
    def direction(run: str):
        opens = sum(1 for c in run if c in "‘“")
        closes = sum(1 for c in run if c in "’”")
        if opens > closes and closes <= 1 and run[0] in "‘“":
            return "open"
        if closes > opens and opens <= 1 and run[-1] in "’”":
            return "close"
        return None

    def fix(m):
        d = direction(m.group(0))
        if d is None:
            return m.group(0)
        report.quote_runs_repaired += 1
        return "“" if d == "open" else "”"

    text = re.sub(r"[‘’“”]{2,}", fix, text)
    before = text
    text = re.sub(r"([‘“])\s+(?=\S)", r"\1", text)                          # open quote + space
    text = re.sub(r"(?<=\S)\s+([’”])(?=[\s.,;:!?—)\]]|$)", r"\1", text)     # space + close quote
    report.punctuation_spaces_fixed += int(text != before)
    return text


_SPACE_BEFORE_PUNCT_RE = re.compile(r"(?<=\S) +([?!;:])(?=\s|$|[”’)\]])")
_ELLIPSIS_RE = re.compile(r"\.(?:\s*\.){2,}")


def _repair_text(text: str, report: Report) -> str:
    text = _repair_quotes(text, report)
    report.punctuation_spaces_fixed += len(_SPACE_BEFORE_PUNCT_RE.findall(text))
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    report.ellipses_repaired += len(_ELLIPSIS_RE.findall(text))
    text = _ELLIPSIS_RE.sub(lambda m: "." * m.group(0).count("."), text)
    for name, pattern, replacement in _OCR_FIXES:
        text, n = pattern.subn(replacement, text)
        if n:
            report.ocr_fixes[name] += n
    return re.sub(r" {2,}", " ", text).strip()


_LEAD_IN_RE = re.compile(r"^([“‘\"']?)((?:[A-Z]+\s+){0,2}[A-Z]{2,})(?=\s+[a-z])")


def _fix_lead_in(text: str, report: Report) -> str:
    """Chapter openings are printed in capitals ("ONE evening", "A GOVERNMENT INSPECTOR
    turned up"): sentence-case the capitalised run."""
    m = _LEAD_IN_RE.match(text)
    if not m:
        return text
    words = m.group(2).split()
    fixed = " ".join([words[0].capitalize()] + [w.lower() if len(w) > 1 else w for w in words[1:]])
    report.lead_ins_fixed += 1
    report.lead_ins.append(f"{m.group(2)} -> {fixed}")
    return m.group(1) + fixed + text[m.end():]


def _looks_prepassed(pages: list) -> bool:
    """True when the text already has the pre-pass's shape: blank-line separated
    blocks that are single paragraphs (one line) or short heading blocks. Raw OCR
    pages have many lines, so this never fires on an unprocessed book."""
    if not pages:
        return False
    multi = 0
    for page in pages:
        if len(page.items) <= 1:
            continue
        if all(_is_caps_line(l, min_letters=2) or re.fullmatch(r"(?i)by\s+\S.{0,38}", l.strip()) for l in page.items):
            continue
        multi += 1
    return multi == 0 and len(pages) >= 2


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def prepass_text(text: str) -> tuple:
    """Return (cleaned text, Report). Idempotent on its own output."""
    report = Report(chars_before=len(text))
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", " ").replace("​", "")
    lines = [l.rstrip() for l in text.split("\n")]

    pages = _split_pages(lines)
    report.pages = len(pages)
    if _looks_prepassed(pages):
        cleaned = "\n\n".join("\n".join(p.items) for p in pages) + "\n"
        report.already_prepassed = True
        report.chars_after = len(cleaned)
        return cleaned, report

    numbers: list = []
    for page in pages:
        _strip_page_end(page, report, numbers)
        _extract_footnote(page, report)
    confirmed_headers = _resolve_headings(pages, report)
    _number_chapters(pages, report)
    for a, b in zip(numbers, numbers[1:]):
        if b != a + 1:
            report.page_number_gaps.append((a, b))

    # Flatten pages: running text lines and standalone headings.
    flat = []
    for page in pages:
        flat.extend(page.items)
    for k, item in enumerate(flat):
        if not isinstance(item, str):
            continue
        if _is_junk_line(item):
            report.removed.append(("junk fragment", item.strip()))
            flat[k] = None
        elif _is_caps_line(item, min_letters=3) and _norm_key(item) in confirmed_headers:
            # A running header stranded mid-page (the page break before it was lost).
            report.removed.append(("running header (mid-page)", item.strip()))
            report.headers_removed[item.strip()] += 1
            flat[k] = None
        elif _is_caps_line(item, min_letters=4):
            heading = [item.strip()]
            if (k + 1 < len(flat) and isinstance(flat[k + 1], str)
                    and re.fullmatch(r"(?i)by\s+\S.{0,38}", flat[k + 1].strip())):
                heading.append(flat[k + 1].strip())
                flat[k + 1] = None
            flat[k] = Heading(heading, kind="caps")
            report.unconfirmed_headers.append(item.strip())
    flat = [x for x in flat if x is not None]

    solid, hyphenated = _build_word_index([x for x in flat if isinstance(x, str)])

    out_blocks: list = []
    run: list = []
    run_label = "start"

    def flush():
        nonlocal run
        if not run:
            return
        joined = _join_hyphens(run, solid, hyphenated, report)
        blocks, wrapped = _provisional_blocks(joined, report)
        report.runs.append((run_label, len([x for x in joined if isinstance(x, str)]), wrapped))
        out_blocks.extend(_repair_text(block, report) for block in blocks)
        run = []

    for item in flat:
        if isinstance(item, Heading):
            flush()
            out_blocks.append("\n".join(item.lines))
            run_label = " ".join(item.lines)
        else:
            run.append(item)
    flush()

    # Lead-in capitals ("ONE evening") right after a chapter heading.
    result, after_chapter = [], False
    for block in out_blocks:
        if after_chapter and not block.startswith("CHAPTER "):
            block = _fix_lead_in(block, report)
        after_chapter = block.startswith("CHAPTER ") and "\n" in block
        result.append(block)

    cleaned = "\n\n".join(b for b in result if b.strip()) + "\n"
    report.blocks = len(result)
    report.oversized_blocks = sum(1 for b in result if len(b) > 2 * SOFT_BLOCK_CHARS)
    report.chars_after = len(cleaned)
    return cleaned, report


def prepass_file(input_file: Path, output_file: Path) -> Report:
    """Read `input_file` (UTF-8), write the pre-passed text to `output_file` (UTF-8,
    LF line endings on every platform) and return the Report. The input is never modified."""
    text = Path(input_file).read_text(encoding="utf-8")
    cleaned, report = prepass_text(text)
    output_file = Path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(cleaned)
    return report
