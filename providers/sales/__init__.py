from .ingramspark import IngramSparkFormat
from .kdp import KDPFormat

FORMATS = [IngramSparkFormat, KDPFormat]


def detect(header_row: list):
    """Auto-detect which platform a CSV came from, from its own header row.
    Returns None if no known format recognizes it — the caller (
    `engines/sales_ingest.py`) should then ask for an explicit --format.
    """
    return next((f for f in FORMATS if f.matches(header_row)), None)


def get(name: str):
    """Look up a format by its explicit --format name. Returns None if unknown."""
    return next((f for f in FORMATS if f.name == name), None)
