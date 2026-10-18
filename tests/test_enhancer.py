"""
Unit and Integration Tests for Deterministic FAA Approach Plate PDF & PNG Enhancer.

Verifies:
1. Strict non-destructive RGBA compositing (100% untouched base pixels outside
   highlight regions, exact alpha-compositing inside highlight regions).
2. Native PyMuPDF vector extraction of flight-path corridors (`lw=1.44`),
   curved dashed missed-approach tracks (`lw=0.72`), rotated text quads,
   and 16-Bezier MSA circles on official FAA d-TPP PDFs.
3. XML-backed `DTPPPlateBrowser` indexing `d-TPP_Metafile.xml` and PDF folders.
4. All 4-Color Tactical Briefing deterministic rules (CYAN, MAGENTA, AMBER, RED).
5. Markdown "5 A's" pilot briefing generator and CLI end-to-end execution.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from cli import enhance_pdf_plate, enhance_plate_image, main as cli_main
from enhancer.compositor import export_enhanced_pdf_bytes, verify_base_pixel_integrity
from enhancer.ocr_engine import preprocess_for_ocr
from enhancer.plate_browser import DEFAULT_FAA_PDF_DIR, DTPPPlateBrowser
from enhancer.rules import PALETTE
from enhancer.sample_plate import create_sample_faa_plate


def test_palette_rgba_constants():
    assert PALETTE["CYAN"] == (0, 215, 245, 80)
    assert PALETTE["MAGENTA"] == (240, 50, 215, 75)
    assert PALETTE["AMBER"] == (255, 165, 0, 85)
    assert PALETTE["RED"] == (245, 50, 50, 80)


def test_preprocess_for_ocr_does_not_mutate_base():
    img = create_sample_faa_plate(width=800, height=1000)
    orig_bytes = img.tobytes()

    upscaled_gray, upscaled_thresh = preprocess_for_ocr(img, scale_factor=2.0)

    assert img.tobytes() == orig_bytes
    assert upscaled_gray.shape == (2000, 1600)
    assert upscaled_thresh.shape == (2000, 1600)


def test_full_pipeline_on_sample_plate():
    img = create_sample_faa_plate(width=1200, height=1600)
    orig_arr = np.array(img, copy=True)

    composited, overlay, highlights, metadata, briefing_md = enhance_plate_image(
        image=img,
        ocr_backend="pytesseract",
        aircraft_categories=["A", "B"],
        target_approaches=["LPV", "LNAV/VNAV"],
    )

    assert np.array_equal(np.array(img), orig_arr)
    assert verify_base_pixel_integrity(img, composited, overlay)

    colors_found = {h.color_key for h in highlights}
    assert {"CYAN", "MAGENTA", "AMBER", "RED"}.issubset(colors_found)


def test_cli_end_to_end(tmp_path: Path):
    sample_png = tmp_path / "sample_in.png"
    out_png = tmp_path / "enhanced_out.png"
    brief_md = tmp_path / "briefing.md"

    rc = cli_main(
        [
            "--generate-sample",
            str(sample_png),
            "-o",
            str(out_png),
            "--categories",
            "A",
            "B",
            "C",
            "--approaches",
            "LPV",
            "LNAV/VNAV",
            "--briefing",
            str(brief_md),
        ]
    )
    assert rc == 0
    assert sample_png.exists()
    assert out_png.exists()
    assert brief_md.exists()
    assert "Tactical \"5 A's\" Approach Briefing" in brief_md.read_text(encoding="utf-8")


def test_dtpp_xml_sqlite_plate_browser():
    browser = DTPPPlateBrowser(DEFAULT_FAA_PDF_DIR)
    if not browser.is_available:
        return

    # 1. Browse by State
    states = browser.list_states(only_with_iac=True)
    state_codes = [s[0] for s in states]
    assert "CA" in state_codes

    # 2. Browse by City within State
    ca_cities = browser.list_cities(state_code="CA", only_with_iac=True)
    assert "SAN MARTIN" in ca_cities
    assert "SANTA MONICA" in ca_cities

    # 3. Filter airports by State + City + Search Query
    e16_airports = browser.filter_airports(
        state_code="CA", city="SAN MARTIN", search_query="E16"
    )
    assert len(e16_airports) >= 1
    assert e16_airports[0].apt_ident == "E16"

    # 4. Fetch IAP approach charts for E16 and verify PDF exists on disk
    charts = browser.get_charts_for_airport(e16_airports[0].airport_name_id, chart_codes=["IAP"])
    assert len(charts) >= 1
    assert any("RNAV (GPS) RWY 32" in c.chart_name for c in charts)
    assert charts[0].exists
    assert charts[0].file_path.suffix.upper() == ".PDF"

    # 5. Direct plate search
    direct = browser.search_plates_directly("E16 RNAV 32")
    assert len(direct) >= 1
    assert direct[0].file_name.upper() == "09260R32.PDF"


def test_pdf_vector_path_and_text_enhancer():
    browser = DTPPPlateBrowser(DEFAULT_FAA_PDF_DIR)
    if not browser.is_available:
        return

    direct = browser.search_plates_directly("E16 RNAV 32")
    assert direct
    pdf_path = direct[0].file_path

    composited, overlay, highlights, metadata, briefing_md = enhance_pdf_plate(
        pdf_path,
        dpi_scale=3.0,
        aircraft_categories=["A", "B"],
        target_approaches=["LPV", "LNAV/VNAV"],
    )

    rule_ids = {h.rule_id for h in highlights}
    assert "CYAN_COMMS_GROUP" in rule_ids
    assert "MAGENTA_APP_CRS_BOX" in rule_ids
    assert "MAGENTA_PLAN_COURSE_PATH" in rule_ids
    assert "MAGENTA_PROFILE_SLOPE_PATH" in rule_ids
    assert "MAGENTA_MINIMUMS_ROW" in rule_ids
    assert "AMBER_PLAN_ESCAPE_PATH" in rule_ids
    assert "AMBER_PROFILE_ESCAPE_PATH" in rule_ids
    assert "AMBER_MISSED_TEXT" in rule_ids
    assert "AMBER_PROFILE_ICON" in rule_ids
    assert "RED_CLIMB_GRADIENT" in rule_ids
    assert "RED_BRIEFING_NOTE" in rule_ids
    assert "RED_GLIDEPATH_TCH" in rule_ids
    assert "RED_ARRIVAL_NA" in rule_ids
    assert "RED_MSA_CIRCLE" in rule_ids
    assert "RED_MSA_ALTITUDE" in rule_ids

    assert metadata.app_crs == "321"
    assert metadata.holding_fix == "ACETY"
    assert metadata.glidepath_angle == 4.00
    assert "5600" in metadata.msa_altitudes

    # Verify non-destructive enhanced PDF export
    pdf_bytes = export_enhanced_pdf_bytes(pdf_path, overlay)
    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 10000
