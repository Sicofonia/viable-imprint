"""Amazon KDP's royalties report — see `providers/sales/base.py`'s important
caveat: not verified against a real live export. KDP's report is
transaction-level (one row per marketplace/date/title), not pre-aggregated
by period the way IngramSpark's Compensation report appears to be, so
`parse()` aggregates every matching row in one CSV into one entry per
(isbn, currency) — `period_start`/`period_end` are derived from the
earliest and latest transaction date actually present in the file, not
read from a dedicated period column.
"""
from .base import SalesFormat, to_float, to_int


class KDPFormat(SalesFormat):
    name = "kdp"

    @staticmethod
    def matches(header_row: list) -> bool:
        return {"ASIN/ISBN", "Royalty", "Marketplace"}.issubset(set(header_row))

    @staticmethod
    def parse(rows: list) -> list:
        groups = {}  # (isbn, currency) -> {"units": int, "revenue": float, "dates": [str]}
        for row in rows:
            isbn = (row.get("ASIN/ISBN") or "").strip()
            if not isbn:
                continue
            currency = (row.get("Currency") or "USD").strip()
            group = groups.setdefault((isbn, currency), {"units": 0, "revenue": 0.0, "dates": []})
            group["units"] += to_int(row.get("Net Units Sold") or row.get("Units Sold"))
            group["revenue"] += to_float(row.get("Royalty"))
            date_value = row.get("Royalty Date") or row.get("Transaction Date")
            if date_value:
                group["dates"].append(date_value)

        normalized = []
        for (isbn, currency), group in groups.items():
            dates = sorted(group["dates"])
            normalized.append({
                "isbn": isbn,
                "units": group["units"],
                "revenue": round(group["revenue"], 2),
                "currency": currency,
                "period_start": dates[0] if dates else None,
                "period_end": dates[-1] if dates else None,
            })
        return normalized
