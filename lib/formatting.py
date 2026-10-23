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


# ---------------------------------------------------------------------------
# The dashboard's lines (`s3 dashboard`), shared with the terminal UI (ADR 023,
# T4.1) so both word things identically. Moved out of pipeline.py unchanged;
# the golden files in tests/fixtures/dashboard_*.golden.txt guard the output.
# ---------------------------------------------------------------------------

COST_OMITTED_NOTE = " (cost omitted — no pricing configured, or no billable tasks run yet)"


def format_provider_model(entry: dict) -> str:
    provider_model = entry.get("model") or entry.get("provider") or "-"
    if entry.get("model") and entry.get("provider"):
        provider_model = f"{entry['provider']} / {entry['model']}"
    return provider_model


def format_usage(usage) -> str:
    if usage and "total_tokens" in usage:
        return f"{usage['total_tokens']:,} tokens"
    if usage and "characters" in usage:
        return f"{usage['characters']:,} characters"
    return "-"


def task_label(key: str, entry: dict) -> str:
    return key if entry["status"] == "done" else f"{key} (stale)"


def book_total_line(summary: dict) -> str:
    note = "" if summary["cost_usd"] is not None else COST_OMITTED_NOTE
    return (f"Total: {summary['tasks_done']}/{summary['tasks_total']} tasks done, "
            f"{format_duration(summary['duration_seconds'])} compute time, "
            f"{format_cost(summary['cost_usd'])} API cost{note}")


def portfolio_totals_line(summaries: list) -> str:
    total_done = sum(s["tasks_done"] for s in summaries)
    known_costs = [s["cost_usd"] for s in summaries if s["cost_usd"] is not None]
    total_cost = sum(known_costs) if known_costs else None
    total_duration = sum(s["duration_seconds"] for s in summaries)
    avg_cost = (total_cost / len(known_costs)) if known_costs else None
    avg_duration = total_duration / len(summaries) if summaries else 0
    return (f"Totals: {format_cost(total_cost)} API cost, {total_done} tasks completed, "
            f"avg {format_cost(avg_cost)}/title, avg compute time {format_duration(avg_duration)}/title")


def cost_flag_line(slug: str, value: float, avg: float, mult: float) -> str:
    return (f"Flagged: {slug} — API cost {format_cost(value)} is {mult:.1f}x "
            f"the portfolio average ({format_cost(avg)})")


def duration_flag_line(key: str, value: float, avg: float, mult: float) -> str:
    return (f"Flagged: {key} — {format_duration(value)} is {mult:.1f}x the portfolio "
            f"average for this task ({format_duration(avg)})")


MARGIN_EXPLANATION = "Reported margin (revenue minus tracked API cost only)"
