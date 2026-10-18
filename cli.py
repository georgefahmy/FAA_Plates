#!/usr/bin/env python3
"""
CLI Tool for Deterministic FAA Approach Plate PDF & PNG Enhancement (`cli.py`).

Usage:
    python cli.py /path/to/09260R32.PDF -o e16_enhanced.png
    python cli.py --airport E16 --chart "RNAV (GPS) RWY 32" -o e16_rnav32.png
    python cli.py --airport KSJC --chart "ILS OR LOC RWY 30L" -o ksjc_ils30l.pdf
    python cli.py --list-plates --state CA --city "SAN MARTIN"
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List, Union

from PIL import Image

from enhancer.briefing import generate_5a_briefing
from enhancer.compositor import (
    export_enhanced_pdf_bytes,
    render_highlights,
    verify_base_pixel_integrity,
)
from enhancer.cv_layout import detect_plate_layout
from enhancer.ocr_engine import OCREngine
from enhancer.pdf_engine import load_pdf_plate
from enhancer.plate_browser import DEFAULT_FAA_PDF_DIR, DTPPPlateBrowser
from enhancer.rules import RuleEngine
from enhancer.sample_plate import create_sample_faa_plate


def enhance_pdf_plate(
    pdf_source: Union[str, Path, bytes],
    dpi_scale: float = 3.0,
    aircraft_categories: List[str] | None = None,
    target_approaches: List[str] | None = None,
    enabled_colors: set[str] | None = None,
    opacity_scale: float = 1.0,
):
    """
    Full deterministic PDF vector pipeline:
    1. Render locked high-DPI RGBA base image from the FAA d-TPP PDF.
    2. Extract exact vector text lines, rotated quads, and vector paths (`lw=1.44`, `lw=0.72`, Bezier MSA circle).
    3. Evaluate deterministic 4-Color Tactical Briefing rules.
    4. Render morphological flight-path ribbons, rotated quads, and rounded boxes onto a separate RGBA overlay.
    5. Verify 100% untouched base pixel integrity and generate Markdown "5 A's" briefing.
    """
    locked_base, tokens, lines, layout = load_pdf_plate(pdf_source, dpi_scale=dpi_scale)

    rule_engine = RuleEngine(
        aircraft_categories=aircraft_categories or ["A", "B"],
        target_approaches=target_approaches or ["LPV", "LNAV/VNAV"],
        enabled_colors=enabled_colors or {"CYAN", "MAGENTA", "AMBER", "RED"},
    )
    highlights, metadata = rule_engine.evaluate(tokens, lines, layout)

    composited, overlay = render_highlights(
        locked_base,
        highlights,
        opacity_scale=opacity_scale,
    )

    if not verify_base_pixel_integrity(locked_base, composited, overlay):
        raise RuntimeError(
            "CRITICAL SAFETY VIOLATION: Base pixels outside highlight overlay were modified!"
        )

    briefing_md = generate_5a_briefing(
        metadata,
        highlights,
        aircraft_categories=rule_engine.aircraft_categories,
        target_approaches=rule_engine.target_approaches,
    )

    return composited, overlay, highlights, metadata, briefing_md


def enhance_plate_image(
    image: Image.Image,
    ocr_backend: str = "auto",
    aircraft_categories: List[str] | None = None,
    target_approaches: List[str] | None = None,
    enabled_colors: set[str] | None = None,
    opacity_scale: float = 1.0,
):
    """Fallback deterministic pipeline for raster `.png` / `.jpg` plates."""
    locked_base = image.convert("RGBA")
    layout = detect_plate_layout(locked_base)
    ocr = OCREngine(backend=ocr_backend, scale_factor=2.0)
    tokens, lines = ocr.extract(locked_base, vertical_lines_x=layout.vertical_lines_x)

    rule_engine = RuleEngine(
        aircraft_categories=aircraft_categories or ["A", "B"],
        target_approaches=target_approaches or ["LPV", "LNAV/VNAV"],
        enabled_colors=enabled_colors or {"CYAN", "MAGENTA", "AMBER", "RED"},
    )
    highlights, metadata = rule_engine.evaluate(tokens, lines, layout)

    composited, overlay = render_highlights(
        locked_base,
        highlights,
        opacity_scale=opacity_scale,
    )

    if not verify_base_pixel_integrity(locked_base, composited, overlay):
        raise RuntimeError(
            "CRITICAL SAFETY VIOLATION: Base PNG pixels outside highlight overlay were modified!"
        )

    briefing_md = generate_5a_briefing(
        metadata,
        highlights,
        aircraft_categories=rule_engine.aircraft_categories,
        target_approaches=rule_engine.target_approaches,
    )

    return composited, overlay, highlights, metadata, briefing_md


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic FAA Approach Plate PDF & PNG Enhancer "
            "(PyMuPDF Vector Paths + Non-Destructive RGBA Overlay)"
        )
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="Path to input FAA approach plate (.pdf, .png, .jpg)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Path to save the enhanced output (.png or .pdf)",
    )
    parser.add_argument(
        "--plates-dir",
        default=str(DEFAULT_FAA_PDF_DIR),
        help=f"Path to FAA d-TPP PDF directory containing d-TPP_Metafile.xml (default: {DEFAULT_FAA_PDF_DIR})",
    )
    parser.add_argument(
        "--list-plates",
        action="store_true",
        help="List airports/charts from the FAA d-TPP XML catalog filtered by --state, --city, or --airport",
    )
    parser.add_argument(
        "--state",
        help="Filter FAA d-TPP catalog by 2-letter US State code (e.g. CA, TX, FL)",
    )
    parser.add_argument(
        "--city",
        help="Filter FAA d-TPP catalog by City name (e.g. 'SAN MARTIN', 'SANTA MONICA')",
    )
    parser.add_argument(
        "--airport",
        help="Select airport from FAA d-TPP catalog by FAA LID or ICAO (e.g. E16, KSMO, KSJC)",
    )
    parser.add_argument(
        "--chart",
        help="Substring of chart name to select when using --airport (e.g. 'RNAV (GPS) RWY 32')",
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        default=["A", "B"],
        choices=["A", "B", "C", "D"],
        help="Aircraft approach categories to highlight in Minimums table (default: A B)",
    )
    parser.add_argument(
        "--approaches",
        nargs="+",
        default=["LPV", "LNAV/VNAV"],
        help="Target approach types to highlight in Minimums table (default: LPV LNAV/VNAV)",
    )
    parser.add_argument(
        "--colors",
        nargs="+",
        default=["CYAN", "MAGENTA", "AMBER", "RED"],
        choices=["CYAN", "MAGENTA", "AMBER", "RED"],
        help="Active tactical highlight layers (default: CYAN MAGENTA AMBER RED)",
    )
    parser.add_argument(
        "--ocr-backend",
        default="auto",
        choices=["auto", "pytesseract", "rapidocr", "easyocr"],
        help="Fallback OCR backend for raster PNG inputs (default: auto)",
    )
    parser.add_argument(
        "--opacity-scale",
        type=float,
        default=1.0,
        help="Multiplier for standard RGBA alpha values (default: 1.0)",
    )
    parser.add_argument(
        "--briefing",
        help="Optional file path to write the Markdown '5 A's' pilot briefing summary",
    )
    parser.add_argument(
        "--generate-sample",
        help="Generate a synthetic FAA approach plate PNG at this path before enhancing it",
    )
    parser.add_argument(
        "--batch-dir",
        help="Directory to scan and batch enhance all IAC (Instrument Approach) PDF plates",
    )
    parser.add_argument(
        "--batch-output-dir",
        help="Optional output directory for batch enhancement (default: overwrite in-place safely)",
    )
    parser.add_argument(
        "--batch-limit",
        type=int,
        default=None,
        help="Optional maximum number of IAC plates to process (useful for test runs)",
    )
    parser.add_argument(
        "--format",
        default="PDF",
        choices=["PDF", "PNG", "BOTH", "pdf", "png", "both"],
        help="Output file format for batch enhancement: PDF, PNG, or BOTH (default: PDF)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Number of concurrent worker threads for batch processing (default: 6)",
    )
    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.batch_dir:
        from enhancer.batch_engine import discover_iac_plates, run_batch_enhancement
        b_dir = Path(args.batch_dir)
        fmt = args.format.upper()
        print(f"[*] Scanning directory for IAC plates: {b_dir}")
        plates, info = discover_iac_plates(b_dir)
        print(f"[*] Discovery: {info}")
        print(f"[*] Found {len(plates)} matching IAC plate(s) on disk.")
        print(f"[*] Selected Output Format: {fmt}")
        if not plates:
            print("[!] No IAC plates found to enhance.", file=sys.stderr)
            return 1

        if args.batch_limit and args.batch_limit > 0:
            print(f"[*] Limiting batch to first {args.batch_limit} plate(s)")
            plates = plates[:args.batch_limit]

        out_dir = Path(args.batch_output_dir) if args.batch_output_dir else None
        if out_dir:
            print(f"[*] Output directory: {out_dir}")
        else:
            print("[*] Output mode: Safe in-place overwrite (atomic rename)")

        def _cli_progress(p):
            stat = f"✅ {p.last_result.highlights_count} hl" if p.last_result.success else f"❌ {p.last_result.error_message}"
            print(
                f"[{p.completed:5d}/{p.total:5d}] {p.current_item.file_name:<16s} "
                f"({p.speed_per_sec:4.1f} pl/s, ETA: {int(p.eta_seconds)}s) -> {stat}"
            )

        t0 = time.time()
        results = run_batch_enhancement(
            plates,
            output_dir=out_dir,
            output_format=fmt,
            aircraft_categories=args.categories,
            target_approaches=args.approaches,
            enabled_colors=set(args.colors),
            opacity_scale=args.opacity_scale,
            max_workers=args.workers,
            progress_callback=_cli_progress,
        )
        t_tot = time.time() - t0
        successes = sum(1 for r in results if r.success)
        failures = sum(1 for r in results if not r.success)
        print(
            f"\n[+] Batch complete in {t_tot:.1f}s ({len(results)/t_tot if t_tot>0 else 0:.1f} plates/sec)! "
            f"Success: {successes}, Failures: {failures}"
        )
        return 0 if failures == 0 else 1

    if args.list_plates:
        browser = DTPPPlateBrowser(data_dir=args.plates_dir)
        if not browser.is_available:
            print(f"[!] FAA d-TPP XML catalog not found in: {browser.data_dir}", file=sys.stderr)
            return 1
        airports = browser.filter_airports(
            state_code=args.state,
            city=args.city,
            search_query=args.airport,
            only_with_iac=False,
        )
        print(f"[*] Found {len(airports)} matching airport(s) (Cycle {browser.cycle}):")
        for a in airports[:35]:
            print(f"  - {a.display_label}")
            if args.airport or len(airports) <= 5:
                for ch in browser.get_charts_for_airport(a.airport_name_id, chart_codes=["IAP"]):
                    print(f"      * {ch.display_label} -> {ch.file_path}")
        return 0

    input_path: Path | None = None
    if args.generate_sample:
        sample_path = Path(args.generate_sample)
        sample_img = create_sample_faa_plate()
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        sample_img.save(sample_path, format="PNG")
        print(f"[+] Generated synthetic sample FAA plate: {sample_path}")
        input_path = sample_path
    elif args.airport:
        browser = DTPPPlateBrowser(data_dir=args.plates_dir)
        if not browser.is_available:
            print(f"[!] FAA d-TPP XML catalog not found in: {browser.data_dir}", file=sys.stderr)
            return 1
        airports = browser.filter_airports(
            state_code=args.state,
            city=args.city,
            search_query=args.airport,
            only_with_iac=False,
        )
        if not airports:
            print(f"[!] No airport matching '{args.airport}' found.", file=sys.stderr)
            return 1
        apt = airports[0]
        charts = browser.get_charts_for_airport(apt.airport_name_id)
        if args.chart:
            q_chart = args.chart.strip().lower()
            charts = [c for c in charts if q_chart in c.chart_name.lower() or q_chart in c.file_name.lower()]
        else:
            iap_charts = [c for c in charts if c.chart_code in ("IAP", "IAC")]
            if iap_charts:
                charts = iap_charts
        if not charts:
            print(f"[!] No matching charts found for {apt.display_label}.", file=sys.stderr)
            return 1
        chosen = charts[0]
        print(f"[*] Selected from FAA d-TPP catalog: {apt.display_label} -> {chosen.display_label}")
        input_path = chosen.file_path
    elif args.input:
        input_path = Path(args.input)
    else:
        parser.error("Provide an input file path, --airport <CODE>, --list-plates, or --generate-sample.")

    if not input_path.is_file():
        print(f"[!] Error: Input file not found: {input_path}", file=sys.stderr)
        return 1

    output_path = (
        Path(args.output)
        if args.output
        else input_path.with_name(f"{input_path.stem}_enhanced.png")
    )

    is_pdf = input_path.suffix.lower() == ".pdf"
    if is_pdf:
        print(f"[*] Loaded FAA vector PDF plate: {input_path}")
        composited, overlay, highlights, metadata, briefing_md = enhance_pdf_plate(
            input_path,
            dpi_scale=3.0,
            aircraft_categories=args.categories,
            target_approaches=args.approaches,
            enabled_colors=set(args.colors),
            opacity_scale=args.opacity_scale,
        )
    else:
        orig_img = Image.open(input_path).convert("RGBA")
        print(f"[*] Loaded FAA raster plate: {input_path} ({orig_img.width}x{orig_img.height} px, RGBA)")
        composited, overlay, highlights, metadata, briefing_md = enhance_plate_image(
            orig_img,
            ocr_backend=args.ocr_backend,
            aircraft_categories=args.categories,
            target_approaches=args.approaches,
            enabled_colors=set(args.colors),
            opacity_scale=args.opacity_scale,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.suffix.lower() == ".pdf" and is_pdf:
        pdf_bytes = export_enhanced_pdf_bytes(input_path, overlay)
        output_path.write_bytes(pdf_bytes)
        print(f"[+] Saved enhanced vector PDF plate: {output_path}")
    else:
        composited.save(output_path, format="PNG")
        print(f"[+] Saved enhanced tactical PNG plate: {output_path} ({composited.width}x{composited.height} px)")

    print("[+] Base pixel integrity verified: 100% untouched underlying chart")
    print(f"[+] Total deterministic highlights applied: {len(highlights)}")
    for c_key in ("CYAN", "MAGENTA", "AMBER", "RED"):
        count = sum(1 for h in highlights if h.color_key == c_key)
        print(f"    - {c_key:7s}: {count} highlights")

    if args.briefing:
        brief_path = Path(args.briefing)
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(briefing_md, encoding="utf-8")
        print(f"[+] Saved '5 A's' Markdown briefing: {brief_path}")
    else:
        print("\n" + briefing_md)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
