"""One subclass per distribution platform's royalty/sales CSV export. New
platforms are added by writing one new module here and registering it in
`providers/sales/__init__.py`'s `FORMATS` list — `engines/sales_ingest.py`
never names a specific platform. See docs/adr/011-system-3-sales-ingestion.md.

IMPORTANT: the column names each concrete format's `matches()`/`parse()`
expect are this project's best-effort understanding of each platform's
report shape, NOT verified against a real, currently-live downloaded
export — neither platform's exact current CSV layout was available to
check while writing this (same discipline ADR 005 already applies to
pricing: don't assert a fact as verified when it isn't). Both KDP and
IngramSpark have changed their export formats before. If detection fails,
or the numbers look wrong against your own real export, that's the first
thing to check — open the CSV, compare its header row to `matches()`
below, and adjust the column names to match.
"""


class SalesFormat:
    name: str  # e.g. "ingramspark"

    @staticmethod
    def matches(header_row: list) -> bool:
        """True if this format recognizes the CSV's own header row — used
        for auto-detection, so a publisher can drop in whatever export they
        downloaded without saying which platform it's from.
        """
        raise NotImplementedError

    @staticmethod
    def parse(rows: list) -> list:
        """rows: list of dicts (csv.DictReader output, one per CSV row).
        -> normalized dicts: isbn, units, revenue, currency, period_start,
        period_end (dates as 'YYYY-MM-DD' strings). `isbn` is used by the
        engine to filter to one book and then discarded, not stored.
        """
        raise NotImplementedError


def to_int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def to_float(value) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0
