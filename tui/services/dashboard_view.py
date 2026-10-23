"""System 3's dashboard as display rows (ADR 023, T4.1). Plain Python, no Textual.

The figures come from `lib.dashboard` and every string from `lib.formatting`, the
same functions `s3 dashboard` prints with, so the UI shows exactly the CLI's numbers
and wording; only the layout (table cells instead of padded text) is the UI's own.
"""
from dataclasses import dataclass, field

from lib import dashboard, formatting
from tui.services.context import Context

MARGIN_NOTE = (f"Margin (API): {formatting.MARGIN_EXPLANATION}. It leaves out design, freelance editorial time, "
               "ISBN fees and print cost, which are not tracked anywhere in this project (ADR 011).")


@dataclass
class PortfolioRow:
    slug: str
    tasks: str            # "2/13"
    cost: str             # "$0.100 !" when flagged
    compute: str
    revenue: str
    margin: str
    span: str
    flagged: bool = False


@dataclass
class PortfolioView:
    rows: list = field(default_factory=list)
    totals: str = ""
    flags: list = field(default_factory=list)       # "Flagged: ..." lines, as the CLI words them

    @property
    def heading(self) -> str:
        n = len(self.rows)
        return f"Portfolio: {n} book{'s' if n != 1 else ''}"


@dataclass
class TaskMetric:
    label: str            # "s1b.cleanup", or "s1b.cleanup (stale)"
    duration: str         # "1m 40s !" when flagged
    provider_model: str
    usage: str
    cost: str
    flagged: bool = False


@dataclass
class BookView:
    slug: str
    tasks: list = field(default_factory=list)
    total: str = ""
    span: str = ""
    revenue: str = None   # only when the book has revenue on record
    margin: str = None
    flags: list = field(default_factory=list)


def portfolio_view(ctx: Context) -> PortfolioView:
    summaries = dashboard.portfolio_summary(ctx.data_dir("books_dir"))
    if not summaries:
        return PortfolioView()
    flags = dashboard.deviation_flags(summaries, ctx.config)
    rows = []
    for s in summaries:
        flagged = s["slug"] in flags["cost"]
        rows.append(PortfolioRow(
            slug=s["slug"], tasks=f"{s['tasks_done']}/{s['tasks_total']}",
            cost=formatting.format_cost(s["cost_usd"]) + (" !" if flagged else ""),
            compute=formatting.format_duration(s["duration_seconds"]),
            revenue=formatting.format_revenue(s["revenue_by_currency"]),
            margin=formatting.format_margin(s["reported_margin_usd"]),
            span=formatting.format_span(s["span_start"], s["span_end"]), flagged=flagged))
    return PortfolioView(
        rows=rows, totals=formatting.portfolio_totals_line(summaries),
        flags=[formatting.cost_flag_line(slug, *vals) for slug, vals in sorted(flags["cost"].items())])


def book_view(ctx: Context, slug: str) -> BookView:
    books_dir = ctx.data_dir("books_dir")
    summary = dashboard.book_summary(books_dir / slug)
    # Duration outliers are peer-compared across the whole portfolio (ADR 010), so the
    # flags are computed against every book, then narrowed to this one.
    flags = dashboard.deviation_flags(dashboard.portfolio_summary(books_dir), ctx.config)
    duration_flags = {key: vals for (s, key), vals in flags["duration"].items() if s == slug}

    tasks = []
    for key, entry in sorted(summary["tasks"].items()):
        if entry.get("status") not in ("done", "stale"):
            continue
        flagged = key in duration_flags
        tasks.append(TaskMetric(
            label=formatting.task_label(key, entry),
            duration=formatting.format_duration(entry.get("duration_seconds")) + (" !" if flagged else ""),
            provider_model=formatting.format_provider_model(entry),
            usage=formatting.format_usage(entry.get("usage")),
            cost=formatting.format_cost(entry.get("cost_usd")), flagged=flagged))

    view = BookView(slug=slug, tasks=tasks, total=formatting.book_total_line(summary),
                    span=f"In-pipeline span: {formatting.format_span(summary['span_start'], summary['span_end'])}",
                    flags=[formatting.duration_flag_line(key, *vals) for key, vals in sorted(duration_flags.items())])
    if summary["revenue_by_currency"]:
        view.revenue = f"Revenue: {formatting.format_revenue(summary['revenue_by_currency'])}"
        view.margin = f"{formatting.MARGIN_EXPLANATION}: {formatting.format_margin(summary['reported_margin_usd'])}"
    return view
