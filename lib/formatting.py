"""Display formatting for System 3's dashboard figures.

Moved out of `pipeline.py` unchanged (ADR 023, T0.5) so the terminal UI shows
numbers exactly as `s3 dashboard` does. Pure functions: no I/O, no config.
"""
from datetime import date


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds or 0))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def format_cost(cost_usd) -> str:
    if cost_usd is None:
        return "-"
    # Per-task costs for short texts are routinely sub-cent — 3 decimals
    # would silently round them to "$0.000", hiding real signal.
    return f"${cost_usd:.6f}" if cost_usd < 0.01 else f"${cost_usd:.3f}"


def format_margin(margin_usd) -> str:
    if margin_usd is None:
        return "-"
    sign = "-" if margin_usd < 0 else ""
    return f"{sign}${abs(margin_usd):.2f}"


def format_revenue(revenue_by_currency: dict) -> str:
    if not revenue_by_currency:
        return "-"
    # Grouped by currency, not summed across them (ADR 011, point 6) — no
    # FX conversion is attempted anywhere in this project.
    return ", ".join(f"{amount:.2f} {currency}" for currency, amount in sorted(revenue_by_currency.items()))


def format_span(start_iso: str, end_iso: str) -> str:
    if not start_iso:
        return "no runs yet"
    start = date.fromisoformat(start_iso[:10])
    end = date.fromisoformat(end_iso[:10])
    days = (end - start).days
    label = "in progress" if days == 0 else f"{days} day{'s' if days != 1 else ''}"
    return f"{start} -> {end} ({label})"
