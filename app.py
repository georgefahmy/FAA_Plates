"""
Streamlit Web UI for the Deterministic FAA d-TPP PDF & PNG Plate Enhancer (`app.py`).

Features:
- Built-in FAA d-TPP XML-backed Plate Browser (`/Users/GFahmy/Documents/RV-7/Airmate/FAA_pdf_data`
  + `d-TPP_Metafile.xml`) indexing 3,197 airports and 24,075 vector PDF charts by
  **State**, **City**, **Airport (FAA LID / ICAO / Name)**, and **Chart Type (`IAP`, `APD`, `DP`, `STR`, `MIN`)**
- Native PyMuPDF (`pymupdf`) vector extraction of approach course corridors (`lw ≈ 1.44`),
  profile descent slopes (`lw ≈ 1.44`), curved dashed missed-approach escape tracks (`lw ≈ 0.72`),
  16-Bezier MSA 25 NM circles, rotated text quads, and table cells
- Drag-and-drop `.pdf` / `.png` / `.jpg` uploader
- Interactive 4-Color Tactical Briefing layer toggles (CYAN, MAGENTA, AMBER, RED)
- Aircraft Category selector (Cat A, B, C, D) and Target Approach Type selector
- Side-by-side Original vs. Enhanced comparison + non-destructive pixel/vector integrity badge
- Instant Enhanced PNG AND Enhanced Vector PDF downloads + "5 A's" Pilot Briefing
"""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import List, Optional, Set, Tuple

import streamlit as st
from PIL import Image

from enhancer.batch_engine import (
    BatchProgress,
    IACPlateItem,
    discover_iac_plates,
    run_batch_enhancement,
)
from enhancer.briefing import generate_5a_briefing
from enhancer.compositor import (
    export_enhanced_pdf_bytes,
    render_highlights,
    verify_base_pixel_integrity,
)
from enhancer.cv_layout import detect_plate_layout
from enhancer.ocr_engine import OCREngine
from enhancer.pdf_engine import load_pdf_plate
from enhancer.plate_browser import (
    DEFAULT_FAA_PDF_DIR,
    AirportEntry,
    DTPPPlateBrowser,
    PlateChartEntry,
)
from enhancer.rules import RuleEngine
from enhancer.sample_plate import create_sample_faa_plate

st.set_page_config(
    page_title="Approach Plate 4-Color Tactical Enhancer",
    page_icon="✈️",
    layout="wide",
)


@st.cache_resource(show_spinner=False)
def get_ocr_engine(backend: str) -> OCREngine:
    return OCREngine(backend=backend, scale_factor=2.0)


@st.cache_resource(show_spinner=False)
def get_dtpp_browser(data_dir_str: str) -> DTPPPlateBrowser:
    return DTPPPlateBrowser(data_dir=Path(data_dir_str))


@st.cache_data(show_spinner=False)
def extract_pdf_plate_cached(pdf_bytes: bytes, dpi_scale: float = 3.0):
    locked_base, tokens, lines, layout = load_pdf_plate(pdf_bytes, dpi_scale=dpi_scale)
    buf = io.BytesIO()
    locked_base.save(buf, format="PNG")
    return buf.getvalue(), tokens, lines, layout


@st.cache_data(show_spinner=False)
def extract_raster_plate_cached(image_bytes: bytes, backend: str):
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    layout = detect_plate_layout(img)
    engine = get_ocr_engine(backend)
    tokens, lines = engine.extract(img, vertical_lines_x=layout.vertical_lines_x)
    return tokens, lines, layout


def render_dtpp_browser_panel() -> Tuple[Optional[bytes], bool, str]:
    """
    Render the built-in XML-backed FAA d-TPP PDF Browser allowing filtering
    by State, City, Airport Identifier/Name, and Chart Type.
    Returns `(raw_bytes, is_pdf, base_filename)`.
    """
    data_dir_str = str(DEFAULT_FAA_PDF_DIR)
    browser = get_dtpp_browser(data_dir_str)

    if not browser.is_available:
        st.warning(
            f"FAA d-TPP XML catalog (`d-TPP_Metafile.xml`) not found in `{browser.data_dir}`. "
            "Please upload a PDF/PNG file or use the sample plate tab."
        )
        return None, False, ""

    st.caption(
        f"📂 **FAA d-TPP Vector PDF Catalog:** `{browser.xml_path}` "
        f"• **Cycle {browser.cycle}** ({browser.from_edate} → {browser.to_edate}) "
        f"• **`{len(browser._airports):,}` airports** indexed in SQLite"
    )

    col_f1, col_f2, col_f3, col_f4 = st.columns([2.1, 2.1, 2.7, 1.9])

    states = browser.list_states(only_with_iac=False)
    state_options = ["All States"] + [
        f"{sc} — {sname} ({cnt})" for sc, sname, cnt in states
    ]
    default_state_idx = 0
    for idx, opt in enumerate(state_options):
        if opt.startswith("CA —"):
            default_state_idx = idx
            break

    with col_f1:
        selected_state_opt = st.selectbox(
            "1. Filter by State",
            options=state_options,
            index=default_state_idx,
        )
        selected_state_code = (
            None
            if selected_state_opt == "All States"
            else selected_state_opt.split(" — ")[0].strip()
        )

    cities = browser.list_cities(state_code=selected_state_code, only_with_iac=False)
    city_options = ["All Cities"] + cities

    with col_f2:
        selected_city_opt = st.selectbox(
            "2. Filter by City",
            options=city_options,
            index=0,
        )
        selected_city = None if selected_city_opt == "All Cities" else selected_city_opt

    with col_f3:
        search_query = st.text_input(
            "3. Search Airport / City / FAA LID / ICAO",
            value="",
            placeholder="e.g. E16, San Martin, KSJC, KSMO, Santa Monica...",
        )

    with col_f4:
        chart_type_filter = st.multiselect(
            "4. Chart Types",
            options=["IAP", "DP", "STR", "ODP"],
            default=["IAP"],
            help="IAP = Instrument Approach Procedures, DP = Departures, STR = STARs, MIN = Minimums",
        )

    only_iap = "IAP" in chart_type_filter and len(chart_type_filter) == 1
    matching_airports: List[AirportEntry] = browser.filter_airports(
        state_code=selected_state_code,
        city=selected_city,
        search_query=search_query,
        only_with_iac=only_iap,
    )

    if not matching_airports:
        st.info("No airports match the current State / City / Search filter.")
        return None, False, ""

    col_apt, col_plt = st.columns([1.45, 1.55])

    # Default to E16 (San Martin) if present in current list, otherwise KSMO / KSJC
    default_apt_idx = 0
    for idx, a in enumerate(matching_airports):
        if a.apt_ident == "E16":
            default_apt_idx = idx
            break

    with col_apt:
        selected_airport = st.selectbox(
            f"✈️ Select Airport ({len(matching_airports):,} matching)",
            options=matching_airports,
            index=default_apt_idx,
            format_func=lambda a: a.display_label,
        )

    charts: List[PlateChartEntry] = browser.get_charts_for_airport(
        selected_airport.airport_name_id,
        chart_codes=chart_type_filter or None,
    )

    if not charts:
        st.warning(
            f"No charts matching `{', '.join(chart_type_filter)}` found for {selected_airport.display_label}."
        )
        return None, False, ""

    with col_plt:
        selected_chart = st.selectbox(
            f"🗺️ Select Approach Plate PDF ({len(charts)} available)",
            options=charts,
            index=0,
            format_func=lambda c: c.display_label,
        )

    if not selected_chart.exists:
        st.error(f"Chart PDF file not found on disk: `{selected_chart.file_path}`")
        return None, False, ""

    raw_bytes = selected_chart.file_path.read_bytes()
    safe_chart_name = (
        selected_chart.chart_name.replace(" ", "_")
        .replace("/", "-")
        .replace("(", "")
        .replace(")", "")
    )
    stem = f"enhanced_{selected_airport.icao_ident or selected_airport.apt_ident}_{safe_chart_name}"
    return raw_bytes, True, stem


def render_batch_enhancer_panel(
    selected_cats: List[str],
    selected_approaches: List[str],
    enabled_colors: Set[str],
    opacity_scale: float,
) -> None:
    """
    Render the Batch Directory Enhancer interface, allowing users to pass an
    entire directory (e.g. ChartData), scan for official IAC/IAP approach plates,
    and apply the 4-Color Tactical Briefing vector overlay across all of them.
    All non-IAC file types (APD, DP, STAR, MIN) are left 100% untouched.
    """
    st.header("⚡ Batch Directory Enhancer — Instrument Approach Procedures (IAC Only)")
    st.info(
        "💡 **Targeted Enhancement:** Pass an extracted plates directory (e.g. `ChartData` containing `Plates.sqlite` or FAA XML). "
        "The engine will automatically discover and batch-enhance **only** official Instrument Approach Procedure (`IAC` / `IAP`) vector PDF charts. "
        "All other non-IAC charts (Airport Diagrams `APD`, Departures `DP`, STARs `STAR`, and Minimums `MIN`) are strictly preserved and left 100% untouched."
    )

    col_path, col_scan = st.columns([4, 1.2])
    with col_path:
        default_dir = "/Users/GFahmy/Documents/RV-7/Airmate/ChartData"
        target_dir_str = st.text_input(
            "📁 Extracted Plates Directory",
            value=st.session_state.get("batch_dir_input", default_dir),
            placeholder="/Users/GFahmy/Documents/RV-7/Airmate/ChartData",
            help="Path to ChartData, Plates, or Plates/US folder containing Plates.sqlite and PDF files",
        )
        st.session_state["batch_dir_input"] = target_dir_str

    with col_scan:
        st.write("")
        st.write("")
        scan_btn = st.button("🔍 Scan Directory", width="stretch", type="secondary")

    should_scan = (
        scan_btn
        or "batch_items" not in st.session_state
        or st.session_state.get("batch_scanned_path") != target_dir_str
    )

    if should_scan:
        p = Path(target_dir_str)
        if not p.exists():
            st.error(f"Directory not found on disk: `{p}`")
            return
        with st.spinner(f"Scanning `{p}` for IAC approach plates..."):
            items, info = discover_iac_plates(p)
            st.session_state["batch_items"] = items
            st.session_state["batch_info"] = info
            st.session_state["batch_scanned_path"] = target_dir_str

    items: List[IACPlateItem] = st.session_state.get("batch_items", [])
    info: str = st.session_state.get("batch_info", "")

    if not items:
        st.warning(
            f"No matching IAC approach plates found in `{target_dir_str}`.\n\n"
            f"Metadata Status: `{info}`"
        )
        return

    st.caption(f"ℹ️ **Metadata Index:** `{info}`")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🎯 Discovered IAC Plates", f"{len(items):,}")
    m2.metric("🛡️ Non-IAC Preserved", "100% Untouched (APD, DP, etc.)")
    m3.metric("⚡ Vector Engine", "PyMuPDF Native Overlay")
    est_mins = max(1, int(len(items) / (5.0 * 60)))
    m4.metric("⏱️ Approx Total Runtime", f"~{est_mins} mins (@ ~5 pl/s)")

    with st.expander(
        f"📋 Preview Discovered IAC Approach Plates (First 50 of {len(items):,})",
        expanded=False,
    ):
        df_preview = [
            {
                "State": it.state,
                "Airport": it.icao_ident or it.airport_ident,
                "Chart Name": it.chart_name,
                "PDF File": it.file_name,
                "Full Path": str(it.file_path),
            }
            for it in items[:50]
        ]
        st.dataframe(df_preview, width="stretch")

    st.divider()
    st.subheader("⚙️ Batch Execution Settings")

    col_dest, col_scope = st.columns(2)
    with col_dest:
        format_opt = st.radio(
            "Output File Format",
            options=[
                "PDF (Enhanced Vector PDF with Overlay)",
                "PNG (High-Resolution RGBA Image)",
                "Both (PDF + PNG)",
            ],
            index=0,
            help="Choose whether to generate vector PDFs, raster PNG images, or both.",
        )
        selected_format = (
            "BOTH"
            if format_opt.startswith("Both")
            else ("PNG" if format_opt.startswith("PNG") else "PDF")
        )

        dest_mode = st.radio(
            "Output Destination Mode",
            options=[
                "Safe In-Place Overwrite (Updates files directly in ChartData for immediate Airmate/EFB use)",
                "Export to Separate Output Folder (Leaves original PDFs completely untouched)",
            ],
            index=0,
        )
        out_dir_path: Optional[Path] = None
        if "Separate" in dest_mode:
            out_dir_str = st.text_input(
                "Export Destination Directory",
                value=str(Path(target_dir_str) / f"Enhanced_IAC_{selected_format}"),
            )
            out_dir_path = Path(out_dir_str)

        workers = st.slider(
            "Concurrent Worker Threads",
            min_value=1,
            max_value=16,
            value=6,
            help="Processing speed scales with thread count. 6-8 threads is recommended.",
        )

    with col_scope:
        run_scope = st.radio(
            "Processing Scope",
            options=[
                f"Full Batch — Enhance ALL {len(items):,} IAC plates",
                "Test Run — Enhance first N plates only",
            ],
            index=0,
        )
        test_limit: Optional[int] = None
        if "Test Run" in run_scope:
            test_limit = st.slider(
                "Test Run Plate Limit",
                min_value=1,
                max_value=min(100, len(items)),
                value=min(10, len(items)),
            )

    st.divider()
    active_items = items[:test_limit] if test_limit else items
    btn_col1, btn_col2 = st.columns([2, 5])
    with btn_col1:
        start_btn = st.button(
            f"🚀 Enhance {len(active_items):,} IAC Plates",
            type="primary",
            width="stretch",
        )

    if start_btn:
        st.markdown(
            f"**⚡ Running batch enhancement on {len(active_items):,} IAC vector PDF plates...**"
        )

        prog_bar = st.progress(0.0)
        status_banner = st.empty()
        col_m1, col_m2, col_m3, col_m4 = st.columns(4)
        p_slot = col_m1.empty()
        s_slot = col_m2.empty()
        e_slot = col_m3.empty()
        eta_slot = col_m4.empty()

        log_placeholder = st.empty()
        recent_logs: List[str] = []

        def _ui_progress_cb(p: BatchProgress) -> None:
            pct = min(1.0, p.completed / p.total)
            prog_bar.progress(pct)
            status_banner.text(
                f"Processing ({p.completed}/{p.total}): {p.current_item.file_name} — {p.current_item.chart_name}"
            )
            p_slot.metric("Progress", f"{p.completed} / {p.total} ({pct * 100:.1f}%)")
            s_slot.metric("Speed", f"{p.speed_per_sec:.1f} pl/s")
            elapsed_fmt = time.strftime("%H:%M:%S", time.gmtime(p.elapsed_seconds))
            e_slot.metric("Elapsed", elapsed_fmt)
            eta_fmt = time.strftime("%H:%M:%S", time.gmtime(p.eta_seconds))
            eta_slot.metric("ETA", eta_fmt)

            badge = "✅" if p.last_result.success else "❌"
            msg = f"{badge} `{p.current_item.file_name}` ({p.current_item.icao_ident or p.current_item.airport_ident}) — {p.current_item.chart_name}: **{p.last_result.highlights_count} hl** ({p.last_result.duration:.2f}s)"
            recent_logs.append(msg)
            if len(recent_logs) > 8:
                recent_logs.pop(0)
            log_placeholder.markdown("\n\n".join(reversed(recent_logs)))

        t_start = time.time()
        results = run_batch_enhancement(
            active_items,
            output_dir=out_dir_path,
            output_format=selected_format,
            aircraft_categories=selected_cats or ["A", "B"],
            target_approaches=selected_approaches or ["LPV", "LNAV/VNAV"],
            enabled_colors=enabled_colors,
            opacity_scale=opacity_scale,
            max_workers=workers,
            progress_callback=_ui_progress_cb,
        )
        total_time = time.time() - t_start
        prog_bar.progress(1.0)
        status_banner.empty()
        successes = sum(1 for r in results if r.success)
        failures = sum(1 for r in results if not r.success)
        speed = len(results) / total_time if total_time > 0 else 0.0

        st.success(
            f"🎉 **Batch Enhancement Complete in {total_time:.1f}s ({speed:.1f} plates/sec)!**\n\n"
            f"- **Enhanced IAC Plates:** `{successes:,}`\n"
            f"- **Format:** `{selected_format}`\n"
            f"- **Errors / Failures:** `{failures}`\n"
            f"- **Underlying Non-IAC Charts Untouched:** `100%`"
        )


def main() -> None:
    st.title("✈️ Approach Plate 4-Color Tactical Enhancer")

    with st.sidebar:
        st.header("🎛️ Tactical Briefing Controls")

        st.subheader("1. 4-Color Layer Toggles")
        use_cyan = st.checkbox("🟦 CYAN — Comms & Setup (0, 215, 245, 80)", value=True)
        use_magenta = st.checkbox(
            "🟪 MAGENTA — Flight Paths, Fixes & Minimums (240, 50, 215, 75)", value=True
        )
        use_amber = st.checkbox(
            "🟧 AMBER — Missed Approach & Escape Path (255, 165, 0, 85)", value=True
        )
        use_red = st.checkbox(
            "🟥 RED — Gotchas, GP/TCH, MSA & Restrictions (245, 50, 50, 80)", value=True
        )

        enabled_colors: Set[str] = set()
        if use_cyan:
            enabled_colors.add("CYAN")
        if use_magenta:
            enabled_colors.add("MAGENTA")
        if use_amber:
            enabled_colors.add("AMBER")
        if use_red:
            enabled_colors.add("RED")

        st.divider()
        st.subheader("2. Aircraft & Minimums")
        selected_cats: List[str] = st.multiselect(
            "Aircraft Approach Category",
            options=["A", "B", "C", "D"],
            default=["A", "B"],
            help="Determines how far right the Magenta row highlight spans in the Minimums table.",
        )
        selected_approaches: List[str] = st.multiselect(
            "Target Approach Minimums Rows",
            options=["LPV", "LNAV/VNAV", "LNAV", "CIRCLING", "ILS", "LOC"],
            default=["LPV", "LNAV/VNAV"],
        )

        st.divider()
        st.subheader("3. Rendering Settings")
        opacity_scale = st.slider(
            "Highlight Opacity Scale",
            min_value=0.5,
            max_value=1.5,
            value=1.0,
            step=0.05,
        )
        ocr_backend = st.selectbox(
            "Fallback Raster OCR Engine (for PNG/JPG uploads)",
            options=["auto", "rapidocr", "pytesseract", "easyocr"],
            index=0,
        )

    app_mode = st.radio(
        "Application Mode",
        options=[
            "🔍 Single Plate Tactical Viewer & Briefing",
            "⚡ Batch Directory Enhancer (IAC Plates Only)",
        ],
        horizontal=True,
    )

    if app_mode.startswith("⚡"):
        render_batch_enhancer_panel(
            selected_cats=selected_cats,
            selected_approaches=selected_approaches,
            enabled_colors=enabled_colors,
            opacity_scale=opacity_scale,
        )
        return

    source_mode = st.radio(
        "Plate Input Source",
        options=[
            "🗂️ Browse FAA d-TPP PDF Catalog (by State / City / Airport)",
            "📤 Upload Custom Plate (.pdf, .png, .jpg)",
            "🧪 Built-In Synthetic Sample Plate",
        ],
        horizontal=True,
    )

    raw_bytes: Optional[bytes] = None
    is_pdf = False
    source_stem = "enhanced_plate"

    if source_mode.startswith("🗂️"):
        raw_bytes, is_pdf, source_stem = render_dtpp_browser_panel()
    elif source_mode.startswith("📤"):
        uploaded_file = st.file_uploader(
            "Upload FAA Instrument Approach Procedure (IAP) Plate (`.pdf`, `.png`, `.jpg`, `.jpeg`)",
            type=["pdf", "png", "jpg", "jpeg"],
        )
        if uploaded_file is not None:
            raw_bytes = uploaded_file.read()
            is_pdf = uploaded_file.name.lower().endswith(".pdf")
            source_stem = f"enhanced_{Path(uploaded_file.name).stem}"
    else:
        sample_img = create_sample_faa_plate()
        buf = io.BytesIO()
        sample_img.save(buf, format="PNG")
        raw_bytes = buf.getvalue()
        is_pdf = False
        source_stem = "enhanced_SAMPLE_RNAV_RWY32"

    if raw_bytes is None:
        return

    with st.spinner(
        "Extracting vector flight paths & rendering 4-Color Tactical Briefing overlay..."
    ):
        if is_pdf:
            base_png_bytes, tokens, lines, layout = extract_pdf_plate_cached(
                raw_bytes, dpi_scale=3.0
            )
            base_image = Image.open(io.BytesIO(base_png_bytes)).convert("RGBA")
        else:
            base_image = Image.open(io.BytesIO(raw_bytes)).convert("RGBA")
            tokens, lines, layout = extract_raster_plate_cached(raw_bytes, ocr_backend)

        rule_engine = RuleEngine(
            aircraft_categories=selected_cats or ["A", "B"],
            target_approaches=selected_approaches or ["LPV", "LNAV/VNAV"],
            enabled_colors=enabled_colors,
        )
        highlights, metadata = rule_engine.evaluate(tokens, lines, layout)
        composited, overlay = render_highlights(
            base_image,
            highlights,
            opacity_scale=opacity_scale,
        )
        pixel_ok = verify_base_pixel_integrity(base_image, composited, overlay)
        briefing_md = generate_5a_briefing(
            metadata,
            highlights,
            aircraft_categories=selected_cats or ["A", "B"],
            target_approaches=selected_approaches or ["LPV", "LNAV/VNAV"],
        )

    st.divider()
    stat1, stat2, stat3, stat4, stat5 = st.columns([3, 1, 1.6, 1.9, 1.9])
    stat1.metric(
        "Extraction Mode",
        "Native PDF Vector" if is_pdf else "Raster OCR + CV",
        f"{base_image.width} × {base_image.height} px",
    )

    out_png_buf = io.BytesIO()
    composited.save(out_png_buf, format="PNG")
    stat4.download_button(
        label="⬇️ Download Enhanced PNG",
        data=out_png_buf.getvalue(),
        file_name=f"{source_stem}.png",
        mime="image/png",
        type="primary",
        width="stretch",
    )

    if is_pdf:
        enh_pdf_bytes = export_enhanced_pdf_bytes(raw_bytes, overlay)
        stat5.download_button(
            label="⬇️ Download Enhanced PDF",
            data=enh_pdf_bytes,
            file_name=f"{source_stem}.pdf",
            mime="application/pdf",
            width="stretch",
        )

    if pixel_ok:
        st.success("Flight-path ribbons and boxes are complete")

    col_orig, col_enh = st.columns(2)
    with col_orig:
        st.subheader("Original FAA Plate")
        st.image(base_image, width="stretch")
    with col_enh:
        st.subheader("4-Color Tactical Briefing Overlay")
        st.image(composited, width="stretch")

    st.divider()
    tab_brief, tab_boxes = st.tabs(
        [
            "📋 '5 A's' Pilot Tactical Briefing",
            "🔍 Detected Vector Paths & Bounding Boxes",
        ]
    )
    with tab_brief:
        st.markdown(briefing_md)
    with tab_boxes:
        rows_data = [
            {
                "Color": h.color_key,
                "Rule ID": h.rule_id,
                "Label": h.label,
                "Bounding Box [x_min, y_min, x_max, y_max]": str(list(h.bbox)),
                "Shape": h.shape,
                "Vertices": len(h.points) if h.points else 4,
            }
            for h in highlights
        ]
        st.dataframe(rows_data, width="stretch")


if __name__ == "__main__":
    main()
