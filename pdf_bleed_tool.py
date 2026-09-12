#!/usr/bin/env python3
"""
pdf_bleed_tool.py — Sets TrimBox/BleedBox on PDFs exported from LibreOffice
to meet print-vendor technical specifications (originally written for
Bibliomanager, a print-on-demand distribution platform, but applicable to
any prepress workflow that requires explicit PDF page boxes).

LibreOffice Writer (and Draw/Impress) only write /MediaBox when exporting to
PDF; they never define /TrimBox or /BleedBox as explicit keys. This script
adds them after export, using pikepdf (no Acrobat or InDesign required).

This is System 1C tooling (see docs/adr/019-system1c-pdf-print-prep-tooling.md):
a manual, non-pipeline step run by hand after a human has reviewed the
exported PDF, immediately before upload to a print-on-demand platform. It is
also exposed as `pipeline.py s1c check|set-interior|set-cover` for a
consistent entry point with every other system; this file stays runnable
standalone too, e.g. for quick debugging outside a `pipeline.py` checkout.

Requires:
    pip install pikepdf

Commands
--------

1) check — inspects a PDF and prints its page boxes in mm (and compares them
   against an expected trim size, if given):

    python pdf_bleed_tool.py check file.pdf
    python pdf_bleed_tool.py check file.pdf --trim-w 152 --trim-h 229

2) set-interior — for the interior/body PDF (normally WITHOUT bleed). Checks
   that the page size matches the final trim size (±0.4mm tolerance by
   default) and writes an explicit /TrimBox on every page:

    python pdf_bleed_tool.py set-interior interior_in.pdf interior_out.pdf \\
        --trim-w 152 --trim-h 229

3) set-cover — for the cover PDF (which ALWAYS needs bleed — 3mm minimum is
   a common vendor requirement). Assumes you already designed the page in
   LibreOffice at final size + bleed (e.g. trim + 3mm per side = trim + 6mm
   total width/height if bleed is equal on every edge; for a cover with a
   spine, total width = front_cover_w*2 + spine + 2*bleed, total height =
   cover_h + 2*bleed). The script centers the TrimBox inside the MediaBox,
   leaving the given bleed around it, and sets BleedBox = the full MediaBox:

    python pdf_bleed_tool.py set-cover cover_in.pdf cover_out.pdf \\
        --trim-w 320 --trim-h 229 --bleed 3

   If your cover has a variable spine width calculated by the print vendor,
   pass it with --spine-w (it gets added to the trim width automatically) —
   so --trim-w only needs to be front+back cover width, and the script
   builds the total trim:

    python pdf_bleed_tool.py set-cover cover_in.pdf cover_out.pdf \\
        --trim-w 304 --trim-h 229 --spine-w 12 --bleed 3

All measurements are in mm unless you pass --unit pt.
"""

import argparse
import sys

import pikepdf
from pikepdf import Array, Name

MM_PER_PT = 25.4 / 72.0
PT_PER_MM = 72.0 / 25.4


def to_pt(value, unit):
    return value if unit == "pt" else value * PT_PER_MM


def to_mm(value_pt):
    return value_pt * MM_PER_PT


def fmt_mm(value_pt):
    return f"{to_mm(value_pt):.2f}mm"


def get_box(page, name, fallback=None):
    box = page.obj.get(Name("/" + name))
    if box is None:
        return fallback
    return [float(v) for v in box]


def box_wh(box):
    return (box[2] - box[0], box[3] - box[1])


def cmd_check(args):
    pdf = pikepdf.open(args.input)
    expected_w_pt = to_pt(args.trim_w, args.unit) if args.trim_w else None
    expected_h_pt = to_pt(args.trim_h, args.unit) if args.trim_h else None
    tol_pt = to_pt(args.tolerance, args.unit)

    print(f"{args.input}  ({len(pdf.pages)} page(s))\n")
    for i, page in enumerate(pdf.pages, start=1):
        media = get_box(page, "MediaBox")
        crop = get_box(page, "CropBox", media)
        trim = get_box(page, "TrimBox", crop)
        bleed = get_box(page, "BleedBox", crop)
        art = get_box(page, "ArtBox", crop)

        has_trim_key = Name("/TrimBox") in page.obj
        has_bleed_key = Name("/BleedBox") in page.obj

        mw, mh = box_wh(media)
        print(f"Page {i}:")
        print(f"  MediaBox : {fmt_mm(mw)} x {fmt_mm(mh)}")
        print(f"  TrimBox  : {fmt_mm(box_wh(trim)[0])} x {fmt_mm(box_wh(trim)[1])}"
              f"  {'(explicit)' if has_trim_key else '(inherited from MediaBox — not defined in the PDF)'}")
        print(f"  BleedBox : {fmt_mm(box_wh(bleed)[0])} x {fmt_mm(box_wh(bleed)[1])}"
              f"  {'(explicit)' if has_bleed_key else '(inherited from MediaBox — not defined in the PDF)'}")

        if expected_w_pt and expected_h_pt:
            tw, th = box_wh(trim)
            dw, dh = abs(tw - expected_w_pt), abs(th - expected_h_pt)
            ok = dw <= tol_pt and dh <= tol_pt
            status = "OK within tolerance" if ok else "OUT OF TOLERANCE"
            print(f"  Expected trim: {args.trim_w}{args.unit} x {args.trim_h}{args.unit}"
                  f"  -> difference: {to_mm(dw):.2f}mm x {to_mm(dh):.2f}mm  [{status}]")
        print()


def cmd_set_interior(args):
    pdf = pikepdf.open(args.input)
    trim_w_pt = to_pt(args.trim_w, args.unit)
    trim_h_pt = to_pt(args.trim_h, args.unit)
    tol_pt = to_pt(args.tolerance, args.unit)

    problems = []
    for i, page in enumerate(pdf.pages, start=1):
        media = get_box(page, "MediaBox")
        mw, mh = box_wh(media)
        dw, dh = abs(mw - trim_w_pt), abs(mh - trim_h_pt)
        if dw > tol_pt or dh > tol_pt:
            problems.append(
                f"  Page {i}: size {to_mm(mw):.2f}x{to_mm(mh):.2f}mm "
                f"differs from the expected trim {args.trim_w}x{args.trim_h}{args.unit} "
                f"by {to_mm(dw):.2f}x{to_mm(dh):.2f}mm (tolerance {args.tolerance}{args.unit})"
            )
        # Explicit TrimBox = exact trim area, anchored to the page origin
        trim_box = Array([media[0], media[1],
                           media[0] + trim_w_pt, media[1] + trim_h_pt])
        page.obj[Name("/TrimBox")] = trim_box
        page.obj[Name("/CropBox")] = media
        page.obj[Name("/ArtBox")] = trim_box

    if problems:
        print("WARNING — some pages don't match the given trim size:", file=sys.stderr)
        for p in problems:
            print(p, file=sys.stderr)
        print(file=sys.stderr)

    pdf.save(args.output)
    print(f"Done: {args.output}  (TrimBox set to {args.trim_w}x{args.trim_h}{args.unit} on "
          f"{len(pdf.pages)} page(s){', see warnings above' if problems else ''})")


def cmd_set_cover(args):
    pdf = pikepdf.open(args.input)
    bleed_pt = to_pt(args.bleed, args.unit)
    trim_w_pt = to_pt(args.trim_w, args.unit) + to_pt(args.spine_w, args.unit)
    trim_h_pt = to_pt(args.trim_h, args.unit)

    expected_media_w = trim_w_pt + 2 * bleed_pt
    expected_media_h = trim_h_pt + 2 * bleed_pt

    if len(pdf.pages) != 1:
        print(f"WARNING: the cover has {len(pdf.pages)} page(s); it should normally be a single page "
              f"(back cover + spine + front cover in one PDF).", file=sys.stderr)

    for page in pdf.pages:
        media = get_box(page, "MediaBox")
        mw, mh = box_wh(media)
        diff_w, diff_h = abs(mw - expected_media_w), abs(mh - expected_media_h)
        if diff_w > 1.0 or diff_h > 1.0:  # >~0.35mm rounding margin
            print(
                "WARNING: the current page size "
                f"({to_mm(mw):.2f}x{to_mm(mh):.2f}mm) does not match the "
                f"expected trim+bleed size ({to_mm(expected_media_w):.2f}x{to_mm(expected_media_h):.2f}mm).\n"
                "  This usually means the canvas in LibreOffice wasn't designed with bleed included\n"
                "  (the background needs to reach the page edge, and the page size should be\n"
                "  trim + 2*bleed). The script will still set the boxes, but double-check the design.",
                file=sys.stderr,
            )

        # BleedBox = the entire page (assumes the artwork already reaches the edge)
        bleed_box = media
        # TrimBox = centered rectangle, inset by `bleed` from each edge
        trim_box = Array([
            media[0] + bleed_pt,
            media[1] + bleed_pt,
            media[2] - bleed_pt,
            media[3] - bleed_pt,
        ])

        page.obj[Name("/BleedBox")] = bleed_box
        page.obj[Name("/TrimBox")] = trim_box
        page.obj[Name("/CropBox")] = media
        page.obj[Name("/ArtBox")] = trim_box

    pdf.save(args.output)
    print(f"Done: {args.output}")
    print(f"  TrimBox : {args.trim_w}+{args.spine_w}{args.unit} x {args.trim_h}{args.unit} "
          f"(centered, {args.bleed}{args.unit} bleed around it)")
    print(f"  BleedBox: full page")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="Inspect the page boxes of a PDF")
    p_check.add_argument("input")
    p_check.add_argument("--trim-w", type=float, default=None, help="Expected trim width")
    p_check.add_argument("--trim-h", type=float, default=None, help="Expected trim height")
    p_check.add_argument("--tolerance", type=float, default=0.4, help="Tolerance (default 0.4mm, a common vendor requirement)")
    p_check.add_argument("--unit", choices=["mm", "pt"], default="mm")
    p_check.set_defaults(func=cmd_check)

    p_int = sub.add_parser("set-interior", help="Set TrimBox on the interior PDF (no bleed)")
    p_int.add_argument("input")
    p_int.add_argument("output")
    p_int.add_argument("--trim-w", type=float, required=True)
    p_int.add_argument("--trim-h", type=float, required=True)
    p_int.add_argument("--tolerance", type=float, default=0.4)
    p_int.add_argument("--unit", choices=["mm", "pt"], default="mm")
    p_int.set_defaults(func=cmd_set_interior)

    p_cov = sub.add_parser("set-cover", help="Set TrimBox and BleedBox on the cover PDF (with bleed)")
    p_cov.add_argument("input")
    p_cov.add_argument("output")
    p_cov.add_argument("--trim-w", type=float, required=True, help="Front+back cover width (WITHOUT spine)")
    p_cov.add_argument("--trim-h", type=float, required=True, help="Cover height")
    p_cov.add_argument("--spine-w", type=float, default=0.0, help="Spine width, added to trim-w")
    p_cov.add_argument("--bleed", type=float, default=3.0, help="Bleed per side (default 3mm)")
    p_cov.add_argument("--unit", choices=["mm", "pt"], default="mm")
    p_cov.set_defaults(func=cmd_set_cover)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
