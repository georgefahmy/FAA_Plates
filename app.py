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

from recreate_airmate_chartdata import (
    DEFAULT_FAA_DATA_DIR,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_XML_PATH,
    RecreateProgress,
    build_chartdata,
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


def render_chartdata_generator_panel(
    selected_cats: List[str],
    selected_approaches: List[str],
    enabled_colors: Set[str],
    opacity_scale: float,
) -> None:
    """
    Render the FAA ChartData & EFB Generator interface, allowing users to configure
    and run recreate_airmate_chartdata directly from the Web UI.
    """
    st.header("⚡ FAA ChartData & Plates Generator (Dynon PNG / Airmate PDF)")
    st.info(
        "💡 **Full EFB Package Generator:** Recreate the standardized `ChartData` directory tree "
        "(`ChartData/Plates/Plates.sqlite` and `ChartData/Plates/US/`) strictly from official FAA data "
        "(`d-TPP_Metafile.xml` + `DDTPPA..E` PDFs). "
        "Outputs Dynon Avionics SkyView-compatible single-page PNGs (with optional 4-Color Tactical Briefing on IAC plates) "
        "or pure single-page FAA Vector PDFs for mobile EFBs (Airmate)."
    )

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("📁 Source & Target Paths")
        xml_path_str = st.text_input(
            "FAA d-TPP XML Metafile",
            value=st.session_state.get("cfg_xml_path", str(DEFAULT_XML_PATH)),
            help="Path to d-TPP_Metafile.xml catalog",
        )
        st.session_state["cfg_xml_path"] = xml_path_str

        faa_dir_str = st.text_input(
            "FAA PDF Source Directory",
            value=st.session_state.get("cfg_faa_dir", str(DEFAULT_FAA_DATA_DIR)),
            help="Directory containing DDTPPA..E folders and DCS volumes",
        )
        st.session_state["cfg_faa_dir"] = faa_dir_str

        out_dir_str = st.text_input(
            "Target ChartData Output Directory",
            value=st.session_state.get("cfg_out_dir", str(DEFAULT_OUTPUT_DIR)),
            help="Destination folder for ChartData/Plates/Plates.sqlite and US/",
        )
        st.session_state["cfg_out_dir"] = out_dir_str

    with col2:
        st.subheader("🎨 Format & Enhancement Mode")
        format_opt = st.radio(
            "Output Plate File Format",
            options=[
                "PNG (Dynon Avionics SkyView compatible)",
                "PDF (Official FAA Vector PDF)",
            ],
            index=0,
            help="Dynon SkyView requires raster PNG files named in Plates.sqlite. Mobile EFBs like Airmate use PDF.",
        )
        selected_format = "png" if format_opt.startswith("PNG") else "pdf"

        if selected_format == "png":
            png_mode_opt = st.radio(
                "PNG Enhancement Mode",
                options=[
                    "Enhanced (4-Color Tactical Briefing on IAC plates; standard raster for all others)",
                    "Standard (Crisp standard raster for all plates, no tactical overlays)",
                ],
                index=0,
                help="Enhanced mode briefings apply only to Instrument Approach Charts (IAC). Airport diagrams, STARs, departures, and minimums remain clean raster images.",
            )
            selected_png_mode = "enhanced" if png_mode_opt.startswith("Enhanced") else "standard"

            dpi_val = st.slider(
                "Raster Resolution (DPI)",
                min_value=150,
                max_value=300,
                value=200,
                step=10,
                help="200 DPI matches Dynon SkyView display specs (approx 1076x1650 px).",
            )
            selected_link_mode = "hardlink"
        else:
            selected_png_mode = "standard"
            dpi_val = 200
            link_mode_opt = st.radio(
                "File Linking Strategy",
                options=[
                    "Hardlink (Fastest, zero additional disk space)",
                    "Copy (Physical standalone copy)",
                    "Symlink (Symbolic filesystem link)",
                ],
                index=0,
            )
            selected_link_mode = (
                "hardlink" if link_mode_opt.startswith("Hardlink")
                else ("copy" if link_mode_opt.startswith("Copy") else "symlink")
            )

    st.divider()
    st.subheader("⚙️ Scope & Concurrency")
    col_scope, col_adv = st.columns(2)

    with col_scope:
        scope_opt = st.radio(
            "Processing Scope",
            options=[
                "Full Batch — Process ALL ~22,000+ plates",
                "Test Run — Process first N plates only (Quick verification)",
            ],
            index=0,
        )
        test_limit: Optional[int] = None
        if "Test Run" in scope_opt:
            test_limit = st.slider("Test Run Plate Limit", min_value=5, max_value=200, value=20, step=5)

        workers = st.slider(
            "Concurrent Worker Threads",
            min_value=1,
            max_value=16,
            value=8,
            help="Multi-threading speeds up PNG rasterization and enhancement significantly.",
        )

    with col_adv:
        with st.expander("🛠️ Advanced Options", expanded=False):
            include_afd = st.checkbox(
                "Include Chart Supplements (AFD) from DCS volumes",
                value=True,
                help="Parses DCS Chart Supplement XMLs and includes AFD airport facility pages.",
            )
            clean_dir = st.checkbox(
                "Clean output directory before building",
                value=False,
                help="Wipes existing destination directory prior to running.",
            )
            chart_code_style = st.selectbox(
                "Chart Code Naming Convention",
                options=["airmate (IAC, STAR, DPO, APD, DP, MIN, HOT)", "faa (IAP, STR, ODP, APD, DP, MIN, HOT)"],
                index=0,
            )
            style_code = "airmate" if chart_code_style.startswith("airmate") else "faa"

            ref_db_input = st.text_input(
                "Optional Reference Database (for GPS georeferencing coordinates)",
                value="",
                placeholder="Leave blank for 100% pure FAA data with no external dependency",
            )
            ref_db_path = Path(ref_db_input.strip()) if ref_db_input.strip() else None

    st.divider()

    btn_col1, btn_col2 = st.columns([2, 5])
    with btn_col1:
        run_btn = st.button(
            "🚀 Generate ChartData & Plates",
            type="primary",
            width="stretch",
        )

    if run_btn:
        xml_p = Path(xml_path_str)
        faa_p = Path(faa_dir_str)
        out_p = Path(out_dir_str)

        if not xml_p.is_file():
            st.error(f"FAA d-TPP XML metafile not found: `{xml_p}`")
            return
        if not faa_p.is_dir():
            st.error(f"FAA PDF data directory not found: `{faa_p}`")
            return

        st.markdown(
            f"**⚡ Building ChartData in `{out_p}` (Format: `{selected_format.upper()}`, Mode: `{selected_png_mode.upper()}`) ...**"
        )

        prog_bar = st.progress(0.0)
        status_banner = st.empty()
        m_col1, m_col2, m_col3, m_col4 = st.columns(4)
        c_slot = m_col1.empty()
        s_slot = m_col2.empty()
        e_slot = m_col3.empty()
        eta_slot = m_col4.empty()

        log_placeholder = st.empty()
        recent_logs: List[str] = []

        def _ui_cb(p: RecreateProgress) -> None:
            pct = min(1.0, p.completed / max(1, p.total)) if p.total > 0 else 0.0
            prog_bar.progress(pct)
            status_banner.markdown(f"**Stage:** `{p.stage.upper()}` — {p.message}")

            c_slot.metric("Progress", f"{p.completed:,} / {p.total:,} ({pct * 100:.1f}%)")
            s_slot.metric("Speed", f"{p.speed_per_sec:.1f} pl/s")
            elapsed_fmt = time.strftime("%H:%M:%S", time.gmtime(p.elapsed_seconds))
            e_slot.metric("Elapsed", elapsed_fmt)
            eta_fmt = time.strftime("%H:%M:%S", time.gmtime(p.eta_seconds))
            eta_slot.metric("ETA", eta_fmt)

            if p.current_item:
                badge = "✅" if p.is_success else "❌"
                recent_logs.append(f"{badge} `{p.current_item}` ({p.speed_per_sec:.1f} pl/s)")
                if len(recent_logs) > 8:
                    recent_logs.pop(0)
                log_placeholder.markdown("\n\n".join(reversed(recent_logs)))

        try:
            summary = build_chartdata(
                xml_path=xml_p,
                faa_data_dir=faa_p,
                output_dir=out_p,
                ref_db_path=ref_db_path,
                output_format=selected_format,
                png_mode=selected_png_mode,
                dpi=dpi_val,
                workers=workers,
                link_mode=selected_link_mode,
                include_afd=include_afd,
                chart_code_style=style_code,
                clean=clean_dir,
                limit=test_limit,
                aircraft_categories=selected_cats or ["A", "B"],
                target_approaches=selected_approaches or ["LPV", "LNAV/VNAV"],
                enabled_colors=enabled_colors,
                opacity_scale=opacity_scale,
                progress_callback=_ui_cb,
            )

            prog_bar.progress(1.0)
            status_banner.empty()

            st.success(
                f"🎉 **ChartData Successfully Built in {summary['total_time']:.1f}s!**\n\n"
                f"- **Output Directory:** `{summary['output_dir']}`\n"
                f"- **Plates Database:** `{summary['db_path']}` ({summary['db_size']:,} bytes)\n"
                f"- **Format:** `{summary['output_format'].upper()}` ({'Dynon Avionics SkyView' if summary['output_format'] == 'png' else 'FAA Vector PDF'})\n"
                f"- **Airports Indexed:** `{summary['total_airports']:,}`\n"
                f"- **Charts in Database:** `{summary['total_charts']:,}` (d-TPP: `{summary['total_charts'] - summary['afd_charts_count']:,}`, AFD: `{summary['afd_charts_count']:,}`)\n"
                f"- **Files Processed:** `{summary['completed_files']:,}` (Errors: `{summary['errors']}`)\n"
                f"- **Cycle:** `{summary['cycle']}` ({summary['valid_dates'][0]} → {summary['valid_dates'][1]})"
            )
        except Exception as e:
            st.error(f"❌ Error during ChartData generation: {e}")


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
            "⚡ FAA ChartData & Plates Generator (Dynon PNG / Airmate PDF)",
        ],
        horizontal=True,
    )

    if app_mode.startswith("⚡"):
        render_chartdata_generator_panel(
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
