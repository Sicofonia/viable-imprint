"""System 3's `sales-ingest` task: reads a royalty/sales CSV a publisher
manually downloaded from a distribution platform, auto-detects which
platform it came from (or takes an explicit override), normalizes it, and
appends new (platform, period) entries to the book's own manifest.yaml
`sales:` block — deduped, never overwritten. See
docs/adr/011-system-3-sales-ingestion.md.

Unlike every other book-scoped task, this one's CLI command is hand-written
in pipeline.py, not `_build_command()`-generated — its input file is an
external download, not something already living inside the book's folder,
so the standard "walk up from the file to find manifest.yaml" resolution
doesn't apply (the book is named explicitly instead). See the ADR, point 4.
"""
import csv
import io
from datetime import date, datetime, timezone
from pathlib import Path

import click

import providers.sales as sales_formats
from lib import manifest

CLI_ARG = "file"


def run(input_file: Path, root: Path, system: str, output_name: str, config: dict,
        *, format: str = None) -> Path:
    # utf-8-sig: tolerate a UTF-8 byte-order mark, common in exports from
    # spreadsheet-adjacent tools.
    text = input_file.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    header = reader.fieldnames or []

    if format:
        fmt = sales_formats.get(format)
        if fmt is None:
            known = ", ".join(f.name for f in sales_formats.FORMATS)
            raise click.ClickException(f"Unknown --format {format!r}. Known formats: {known}")
    else:
        fmt = sales_formats.detect(header)
        if fmt is None:
            known = ", ".join(f.name for f in sales_formats.FORMATS)
            raise click.ClickException(
                f"Could not recognize this CSV's format automatically "
                f"(columns: {', '.join(header)}).\n"
                f"Pass --format explicitly. Known formats: {known}"
            )

    isbn = manifest.load(root).get("isbn")
    if not isbn:
        raise click.ClickException(
            "This book has no ISBN on record. Run "
            "`pipeline.py book set-isbn <book_slug> <isbn>` first."
        )

    normalized = fmt.parse(rows)
    matched = [r for r in normalized if r["isbn"] == isbn]
    now = _now_iso()
    entries = [
        {
            "platform": fmt.name,
            "period_start": r["period_start"],
            "period_end": r["period_end"],
            "units": r["units"],
            "revenue": r["revenue"],
            "currency": r["currency"],
            "ingested_at": now,
        }
        for r in matched
    ]
    added, skipped = manifest.record_sale(root, entries)

    run_dir = root / system / output_name / date.today().isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = run_dir / "combined.txt"
    receipt_path.write_text(_render_receipt(input_file, fmt, isbn, entries, added, skipped), encoding="utf-8")

    click.echo(f"  Format: {fmt.name}")
    click.echo(f"  Rows matching ISBN {isbn}: {len(matched)}")
    click.echo(f"  New entries recorded: {added}")
    if skipped:
        click.echo(f"  Already recorded (skipped as duplicates): {skipped}")
    click.echo(f"Saved: {receipt_path}")
    return receipt_path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _render_receipt(input_file: Path, fmt, isbn: str, entries: list, added: int, skipped: int) -> str:
    lines = [
        "Sales ingestion receipt",
        f"Source file: {input_file.name}",
        f"Detected/declared format: {fmt.name}",
        f"Book ISBN: {isbn}",
        f"Rows matched: {len(entries)}",
        f"New entries recorded: {added}",
        f"Already recorded (skipped as duplicates): {skipped}",
        "",
    ]
    for e in entries:
        lines.append(f"- {e['period_start']} to {e['period_end']}: {e['units']} units, "
                      f"{e['revenue']} {e['currency']}")
    return "\n".join(lines)
