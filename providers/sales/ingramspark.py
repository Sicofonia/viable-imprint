"""IngramSpark's Compensation report — see `providers/sales/base.py`'s
important caveat: this column mapping is a best-effort starting point, not
verified against a real live export. Modeled as one row per (ISBN, already-
summarized reporting period) — unlike KDP's transaction-level report, no
aggregation is needed here, only normalization.
"""
from .base import SalesFormat, to_float, to_int


class IngramSparkFormat(SalesFormat):
    name = "ingramspark"

    @staticmethod
    def matches(header_row: list) -> bool:
        return {"ISBN13", "Total Comp"}.issubset(set(header_row))

    @staticmethod
    def parse(rows: list) -> list:
        normalized = []
        for row in rows:
            isbn = (row.get("ISBN13") or "").strip()
            if not isbn:
                continue
            normalized.append({
                "isbn": isbn,
                "units": to_int(row.get("Net Quantity")),
                "revenue": to_float(row.get("Total Comp")),
                "currency": (row.get("Currency") or "USD").strip(),
                "period_start": row.get("Comp Period Start Date"),
                "period_end": row.get("Comp Period End Date"),
            })
        return normalized
