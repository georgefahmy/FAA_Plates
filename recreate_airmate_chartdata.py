#!/usr/bin/env python3
"""
Generate Standard FAA ChartData Folder and Plates.sqlite Strictly from Official FAA Data.

Builds a singular, standardized ChartData structure matching the mobile EFB layout:
    ChartData/
    └── Plates/
        ├── Plates.sqlite
        └── US/
            ├── 00001AD.PDF
            ├── 09260R32.PDF
            ├── EC1ALT01.PDF
            ├── EC1ALT02.PDF
            ├── SW2TO029.PDF
            └── ... (official FAA d-TPP & DCS PDFs, all strictly single-page!)

Built 100% natively from:
- Primary XML: /Users/GFahmy/Documents/RV-7/Airmate/FAA_pdf_data/DDTPPE_261001/d-TPP_Metafile.xml
- FAA PDF Source: /Users/GFahmy/Documents/RV-7/Airmate/FAA_pdf_data (DDTPPA..E and DCS volumes)
- ZERO dependency on external reference data by default.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

@dataclass
class RecreateProgress:
    stage: str  # "indexing", "parsing_xml", "writing_db", "rendering", "complete"
    completed: int
    total: int
    speed_per_sec: float
    elapsed_seconds: float
    eta_seconds: float
    message: str
    current_item: Optional[str] = None
    is_success: bool = True


try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf
    except ImportError:
        print("[!] Error: PyMuPDF ('pymupdf' or 'fitz') is required. Install via: pip install pymupdf")
        sys.exit(1)

try:
    from cli import enhance_pdf_plate
except Exception:
    enhance_pdf_plate = None


DEFAULT_XML_PATH = Path("/Users/GFahmy/Documents/RV-7/Airmate/FAA_pdf_data/DDTPPE_261001/d-TPP_Metafile.xml")
DEFAULT_FAA_DATA_DIR = Path("/Users/GFahmy/Documents/RV-7/Airmate/FAA_pdf_data")
DEFAULT_OUTPUT_DIR = Path("/Users/GFahmy/Documents/RV-7/Airmate/ChartData")

# Mapping of FAA d-TPP chart codes to standard chart categories
CHART_CODE_MAP = {
    "IAP": "IAC",    # Instrument Approach Procedure -> Instrument Approach Chart
    "STR": "STAR",   # Standard Terminal Arrival -> STAR
    "ODP": "DPO",    # Obstacle Departure Procedure -> DPO
    "APD": "APD",    # Airport Diagram
    "DP": "DP",      # Departure Procedure
    "MIN": "MIN",    # Minimums
    "HOT": "HOT",    # Hot Spot
    "LAH": "LAH",    # Land and Hold Short
    "DAU": "IAC",    # AAUP
}

DEFAULT_COUNTRIES = [
    (228, "US", "United States", "K", "NA"),
    (35, "CA", "Canada", "C", "NA"),
    (141, "MX", "Mexico", "MM", "NA"),
    (174, "PR", "Puerto Rico", "TJ", "NA"),
    (238, "VI", "Virgin Islands, U.S.", "TI", "NA"),
    (88, "GU", "Guam", "PG", "OC"),
]


def parse_edate(s: str) -> str:
    """Parse FAA edate string (e.g. '0901Z  10/01/26') into ISO 'YYYY-MM-DD'."""
    m = re.search(r"(\d{2})/(\d{2})/(\d{2})", s)
    if m:
        month, day, yr = m.groups()
        return f"20{yr}-{month}-{day}"
    return ""


def parse_amdtdate(s: str, fallback: str = "") -> str:
    """Parse amendment date string (e.g. '12/05/2019') into ISO 'YYYY-MM-DD'."""
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        month, day, yr = m.groups()
        return f"{yr}-{month}-{day}"
    m2 = re.search(r"(\d{2})/(\d{2})/(\d{2})", s)
    if m2:
        month, day, yr = m2.groups()
        return f"20{yr}-{month}-{day}"
    return fallback


def format_page_filename(stem: str, page_num: int, ext: str = ".png") -> str:
    """
    Format single-page filename according to standard 01, 02 sequencing:
    e.g. EC1ALT + 1 -> EC1ALT01.png (or .PDF)
         EC1ALT + 5 -> EC1ALT05.png
         SW2TO + 29 -> SW2TO029.png
         AKTO + 10 -> AKTO010.png
    """
    return f"{stem}0{page_num}{ext}"


_georef_cache: Dict[Path, Optional[dict]] = {}


def extract_georef_from_pdf(pdf_path: Path) -> Optional[dict]:
    """
    Extract Adobe / OGC Geospatial PDF Viewport (/VP) metadata directly
    from an official FAA d-TPP PDF page and compute top-left (Lat1, Long1)
    and bottom-right (Lat2, Long2) geographic coordinates for Dynon / EFB.
    """
    if not pdf_path or not pdf_path.is_file():
        return None

    if pdf_path in _georef_cache:
        return _georef_cache[pdf_path]

    try:
        doc = pymupdf.open(pdf_path)
        if len(doc) == 0:
            doc.close()
            _georef_cache[pdf_path] = None
            return None

        page = doc[0]
        keys = doc.xref_get_keys(page.xref)
        if "VP" not in keys:
            doc.close()
            _georef_cache[pdf_path] = None
            return None

        vp_str = doc.xref_get_key(page.xref, "VP")[1]
        page_w = float(page.rect.width)
        page_h = float(page.rect.height)
        doc.close()

        gpts_m = re.search(r"/GPTS\s*\[([^\]]+)\]", vp_str)
        bbox_m = re.search(r"/BBox\s*\[([^\]]+)\]", vp_str)
        if not gpts_m or not bbox_m:
            _georef_cache[pdf_path] = None
            return None

        gpts = [float(v) for v in gpts_m.group(1).split()]
        bbox = [float(v) for v in bbox_m.group(1).split()]
        lpts_m = re.search(r"/LPTS\s*\[([^\]]+)\]", vp_str)
        lpts = [float(v) for v in lpts_m.group(1).split()] if lpts_m else [0.1, 0.1, 0.9, 0.1, 0.9, 0.9, 0.1, 0.9]

        bx0, by0, bx1, by1 = bbox
        bw = bx1 - bx0
        bh = by1 - by0
        if bw <= 0 or bh <= 0 or len(gpts) < 8 or len(lpts) < 8:
            _georef_cache[pdf_path] = None
            return None

        lat0, lon0 = gpts[0], gpts[1]  # BL
        lat1, lon1 = gpts[2], gpts[3]  # BR
        lat2, lon2 = gpts[4], gpts[5]  # TR
        lat3, lon3 = gpts[6], gpts[7]  # TL

        cm_m = re.search(r"\"Central_Meridian\",\s*([-\d\.]+)", vp_str)
        lat_o_m = re.search(r"\"Latitude_Of_Origin\",\s*([-\d\.]+)", vp_str)
        sp1_m = re.search(r"\"Standard_Parallel_1\",\s*([-\d\.]+)", vp_str)
        sp2_m = re.search(r"\"Standard_Parallel_2\",\s*([-\d\.]+)", vp_str)

        if cm_m and lat_o_m and sp1_m and sp2_m:
            cm = float(cm_m.group(1))
            lat_o = float(lat_o_m.group(1))
            sp1 = float(sp1_m.group(1))
            sp2 = float(sp2_m.group(1))

            def lcc_fwd(lat, lon):
                a = 6378137.0
                f = 1 / 298.257222101
                e = math.sqrt(2 * f - f * f)
                r_lat, r_lon = math.radians(lat), math.radians(lon)
                r_sp1, r_sp2 = math.radians(sp1), math.radians(sp2)
                r_lat0, r_lon0 = math.radians(lat_o), math.radians(cm)

                def m_fn(phi):
                    return math.cos(phi) / math.sqrt(1 - e * e * math.sin(phi) ** 2)

                def t_fn(phi):
                    return math.tan(math.pi / 4 - phi / 2) / (
                        (1 - e * math.sin(phi)) / (1 + e * math.sin(phi))
                    ) ** (e / 2)

                m1, m2 = m_fn(r_sp1), m_fn(r_sp2)
                t1, t2, t0 = t_fn(r_sp1), t_fn(r_sp2), t_fn(r_lat0)
                n = (math.log(m1) - math.log(m2)) / (math.log(t1) - math.log(t2))
                F = m1 / (n * (t1**n))
                rho0 = a * F * (t0**n)
                t = t_fn(r_lat)
                rho = a * F * (t**n)
                theta = n * (r_lon - r_lon0)
                return rho * math.sin(theta), rho0 - rho * math.cos(theta)

            def lcc_inv(x, y):
                a = 6378137.0
                f = 1 / 298.257222101
                e = math.sqrt(2 * f - f * f)
                r_sp1, r_sp2 = math.radians(sp1), math.radians(sp2)
                r_lat0, r_lon0 = math.radians(lat_o), math.radians(cm)

                def m_fn(phi):
                    return math.cos(phi) / math.sqrt(1 - e * e * math.sin(phi) ** 2)

                def t_fn(phi):
                    return math.tan(math.pi / 4 - phi / 2) / (
                        (1 - e * math.sin(phi)) / (1 + e * math.sin(phi))
                    ) ** (e / 2)

                m1, m2 = m_fn(r_sp1), m_fn(r_sp2)
                t1, t2, t0 = t_fn(r_sp1), t_fn(r_sp2), t_fn(r_lat0)
                n = (math.log(m1) - math.log(m2)) / (math.log(t1) - math.log(t2))
                F = m1 / (n * (t1**n))
                rho0 = a * F * (t0**n)
                rho = math.copysign(math.sqrt(x * x + (rho0 - y) ** 2), n)
                theta = math.atan2(x, rho0 - y)
                t = (rho / (a * F)) ** (1 / n)
                phi = math.pi / 2 - 2 * math.atan(t)
                for _ in range(5):
                    con = e * math.sin(phi)
                    dphi = math.pi / 2 - 2 * math.atan(
                        t * ((1 - con) / (1 + con)) ** (e / 2)
                    ) - phi
                    phi += dphi
                    if abs(dphi) < 1e-10:
                        break
                return math.degrees(phi), math.degrees(r_lon0 + theta / n)

            x0, y0 = lcc_fwd(lat0, lon0)
            x2, y2 = lcc_fwd(lat2, lon2)

            dx_per_lpts = (x2 - x0) / (lpts[4] - lpts[0])
            dy_per_lpts = (y2 - y0) / (lpts[5] - lpts[1])

            lpts_left = -bx0 / bw
            lpts_right = (page_w - bx0) / bw
            lpts_bottom = -by0 / bh
            lpts_top = (page_h - by0) / bh

            x_left = x0 + (lpts_left - lpts[0]) * dx_per_lpts
            x_right = x0 + (lpts_right - lpts[0]) * dx_per_lpts
            y_bottom = y0 + (lpts_bottom - lpts[1]) * dy_per_lpts
            y_top = y0 + (lpts_top - lpts[1]) * dy_per_lpts

            res_lat1, res_lon1 = lcc_inv(x_left, y_top)
            res_lat2, res_lon2 = lcc_inv(x_right, y_bottom)
        else:
            lpts_span_x = lpts[4] - lpts[0]
            lpts_span_y = lpts[5] - lpts[1]
            lpts_left = -bx0 / bw
            lpts_right = (page_w - bx0) / bw
            lpts_bottom = -by0 / bh
            lpts_top = (page_h - by0) / bh

            dlat_dy = (lat3 - lat0) / lpts_span_y
            dlon_dx = (lon1 - lon0) / lpts_span_x

            res_lat1 = lat0 + (lpts_top - lpts[1]) * dlat_dy
            res_lon1 = lon0 + (lpts_left - lpts[0]) * dlon_dx
            res_lat2 = lat0 + (lpts_bottom - lpts[1]) * dlat_dy
            res_lon2 = lon0 + (lpts_right - lpts[0]) * dlon_dx

        res = {
            "Lat1": f"{res_lat1:.7f}",
            "Long1": f"{res_lon1:.7f}",
            "Lat2": f"{res_lat2:.7f}",
            "Long2": f"{res_lon2:.7f}",
            "X1": "0.0",
            "Y1": "0.0",
            "X2": "1.0",
            "Y2": "1.0",
            "Orientation": "0.0",
            "Draw_Left": "0.0",
            "Draw_Top": "0.0",
            "Draw_Right": "1.0",
            "Draw_Bottom": "1.0",
        }
        _georef_cache[pdf_path] = res
        return res
    except Exception:
        _georef_cache[pdf_path] = None
        return None


def render_plate_to_png(
    src_path: Path,
    dst_path: Path,
    page_idx: int = 0,
    is_iac: bool = False,
    enhanced: bool = False,
    dpi: int = 200,
    aircraft_categories: Optional[List[str]] = None,
    target_approaches: Optional[List[str]] = None,
    enabled_colors: Optional[Set[str]] = None,
    opacity_scale: float = 1.0,
) -> bool:
    """
    Render a single PDF page to a crisp PNG image.
    If `enhanced` and `is_iac`, applies the 4-color tactical briefing overlay.
    Otherwise, renders crisp standard raster via PyMuPDF at specified DPI.
    Safe atomic replacement ensures files are never corrupted.
    """
    if dst_path.exists() and dst_path.stat().st_size > 0:
        return True

    try:
        if enhanced and is_iac and enhance_pdf_plate is not None:
            try:
                dpi_scale = max(1.5, dpi / 72.0)
                comp, _, _, _, _ = enhance_pdf_plate(
                    src_path,
                    dpi_scale=dpi_scale,
                    aircraft_categories=aircraft_categories,
                    target_approaches=target_approaches,
                    enabled_colors=enabled_colors,
                    opacity_scale=opacity_scale,
                )
                dst_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = dst_path.with_name(f"tmp_{os.getpid()}_{time.time_ns()}_{dst_path.name}")
                comp.save(tmp_path, format="PNG")
                tmp_path.replace(dst_path)
                return True
            except Exception:
                # Fallback to standard PyMuPDF render if enhancement encounters unusual PDF vector structure
                pass

        doc = pymupdf.open(src_path)
        page = doc[page_idx]
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dst_path.with_name(f"tmp_{os.getpid()}_{time.time_ns()}_{dst_path.name}")
        pix.save(str(tmp_path))
        doc.close()
        tmp_path.replace(dst_path)
        return True
    except Exception as e:
        print(f"[!] Error rendering {src_path} (p.{page_idx}) -> {dst_path}: {e}")
        return False


class FAABookletIndex:
    """
    In-memory text index for official FAA multi-page booklets (Takeoff Minimums,
    Alternate Minimums, Radar Minimums, etc.). Matches airport/city to the exact
    page in the booklet directly from the PDF vector text without any external database.
    """

    def __init__(self, pdf_map: Dict[str, Path]):
        self.pdf_map = pdf_map
        self._cache: Dict[str, List[str]] = {}

    def get_pages_text(self, pdf_name: str) -> List[str]:
        name_u = pdf_name.upper()
        if name_u in self._cache:
            return self._cache[name_u]
        p = self.pdf_map.get(name_u)
        if not p or not p.is_file():
            return []
        try:
            doc = pymupdf.open(p)
            if len(doc) <= 1:
                doc.close()
                return []
            texts = [page.get_text().upper() for page in doc]
            doc.close()
            self._cache[name_u] = texts
            return texts
        except Exception:
            return []

    def find_airport_page(
        self,
        pdf_name: str,
        city: str,
        state: str,
        apt_ident: str,
        icao_ident: str,
    ) -> int:
        pages = self.get_pages_text(pdf_name)
        if not pages:
            return 1

        city_u = city.strip().upper()
        state_u = state.strip().upper()
        city_state = f"{city_u}, {state_u}"

        # 1. Exact "CITY, STATE" header match (official FAA booklet standard format)
        if city_u:
            for idx, txt in enumerate(pages):
                if city_state in txt:
                    return idx + 1

        # 2. City name match
        if len(city_u) >= 4:
            for idx, txt in enumerate(pages):
                if city_u in txt:
                    return idx + 1

        # 3. Airport identifiers
        apt_u = apt_ident.strip().upper()
        icao_u = icao_ident.strip().upper()
        for idx, txt in enumerate(pages):
            if (apt_u and apt_u in txt) or (icao_u and icao_u in txt):
                return idx + 1

        return 1


def index_source_pdfs(faa_data_dir: Path) -> Tuple[Dict[str, Path], Dict[str, int]]:
    """
    Recursively scan `faa_data_dir` for all PDF files (excluding comparison folders).
    Returns:
    - pdf_map: UPPERCASE filename -> source Path
    - page_counts: UPPERCASE filename -> integer page count
    """
    pdf_map: Dict[str, Path] = {}
    page_counts: Dict[str, int] = {}

    print(f"[*] Scanning for FAA PDFs in: {faa_data_dir} ...")
    start_t = time.time()
    for root, dirs, files in os.walk(faa_data_dir):
        if "compare_pdf" in root or "/." in root:
            continue
        for file in files:
            if file.lower().endswith(".pdf"):
                path = Path(root) / file
                name_u = file.upper()
                pdf_map[name_u] = path

    elapsed = time.time() - start_t
    print(f"[+] Found {len(pdf_map):,} unique PDF files in {elapsed:.2f}s. Checking page counts ...")

    t_pages_start = time.time()
    multi_count = 0
    for name_u, path in pdf_map.items():
        try:
            doc = pymupdf.open(path)
            cnt = len(doc)
            doc.close()
            page_counts[name_u] = cnt
            if cnt > 1:
                multi_count += 1
        except Exception:
            page_counts[name_u] = 1

    print(f"[+] Checked page counts in {time.time() - t_pages_start:.2f}s: {multi_count:,} multi-page PDFs identified.")
    return pdf_map, page_counts


def load_optional_reference_db(ref_db_path: Optional[Path]) -> Tuple[
    Dict[str, int],                           # ident -> Airports_Id
    Dict[Tuple[str, str], dict],              # (ident, chart_name) -> georef fields
    Dict[Tuple[str, str], List[Tuple[str, str]]], # (ident, base_stem) -> list of (Chart_Name, File_Name)
    List[tuple],                              # COUNTRIES rows
    List[tuple],                              # REGIONS rows
    int                                       # max_airport_id
]:
    """Optional loader if an external reference DB is explicitly provided via --reference-db."""
    airport_id_map: Dict[str, int] = {}
    georef_map: Dict[Tuple[str, str], dict] = {}
    ref_booklet_map: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    countries_rows: List[tuple] = []
    regions_rows: List[tuple] = []
    max_airport_id = 0

    if not ref_db_path or not ref_db_path.is_file():
        return airport_id_map, georef_map, ref_booklet_map, countries_rows, regions_rows, max_airport_id

    print(f"[*] Loading optional reference DB from: {ref_db_path} ...")
    try:
        conn = sqlite3.connect(str(ref_db_path))
        c = conn.cursor()

        c.execute("SELECT Apt_Ident, ICAO_Ident, Airports_Id FROM Airports")
        for apt, icao, aid in c.fetchall():
            if aid is not None:
                max_airport_id = max(max_airport_id, int(aid))
                if apt:
                    airport_id_map[apt.upper()] = int(aid)
                if icao:
                    airport_id_map[icao.upper()] = int(aid)

        c.execute("""
            SELECT a.Apt_Ident, c.Chart_Name, c.File_Name,
                   c.Lat1, c.Long1, c.Lat2, c.Long2,
                   c.X1, c.Y1, c.X2, c.Y2,
                   c.Orientation, c.Draw_Left, c.Draw_Right, c.Draw_Top, c.Draw_Bottom,
                   c.ValidWithinX, c.ValidWithinY, c.ValidWithinRadius, c.ExcludeAreas
            FROM Charts c
            JOIN Airports a ON c.Airports_Id = a.Airports_Id
        """)
        for row in c.fetchall():
            apt_ident = (row[0] or "").strip().upper()
            chart_name = (row[1] or "").strip()
            file_name = (row[2] or "").strip()
            if not apt_ident:
                continue

            if row[3]:
                georef_map[(apt_ident, chart_name.upper())] = {
                    "Lat1": row[3], "Long1": row[4], "Lat2": row[5], "Long2": row[6],
                    "X1": row[7], "Y1": row[8], "X2": row[9], "Y2": row[10],
                    "Orientation": row[11], "Draw_Left": row[12], "Draw_Right": row[13],
                    "Draw_Top": row[14], "Draw_Bottom": row[15],
                    "ValidWithinX": row[16], "ValidWithinY": row[17],
                    "ValidWithinRadius": row[18], "ExcludeAreas": row[19],
                }

            m_stem = re.match(r"^([A-Z0-9]+?)(?:0\d+|\d+)\.(?:png|pdf)$", file_name, re.IGNORECASE)
            if m_stem:
                base_stem = m_stem.group(1).upper()
                ref_booklet_map.setdefault((apt_ident, base_stem), []).append((chart_name, file_name))

        try:
            c.execute("SELECT ID, CODE, NAME, COUNTRY_AERO, CONTINENT FROM COUNTRIES")
            countries_rows = c.fetchall()
        except sqlite3.OperationalError:
            pass

        try:
            c.execute("SELECT ID, GLOBALCODE, LOCALCODE, NAME, CONTINENT, COUNTRY FROM REGIONS")
            regions_rows = c.fetchall()
        except sqlite3.OperationalError:
            pass

        conn.close()
        print(f"[+] Loaded {len(airport_id_map):,} airport IDs and {len(georef_map):,} georef records from reference DB.")
    except Exception as e:
        print(f"[!] Warning reading reference DB: {e}")

    return airport_id_map, georef_map, ref_booklet_map, countries_rows, regions_rows, max_airport_id


def build_chartdata(
    xml_path: Path,
    faa_data_dir: Path,
    output_dir: Path,
    ref_db_path: Optional[Path] = None,
    output_format: str = "png",
    png_mode: str = "enhanced",
    dpi: int = 200,
    workers: int = 8,
    link_mode: str = "hardlink",
    include_afd: bool = True,
    chart_code_style: str = "airmate",
    clean: bool = False,
    limit: Optional[int] = None,
    aircraft_categories: Optional[List[str]] = None,
    target_approaches: Optional[List[str]] = None,
    enabled_colors: Optional[Set[str]] = None,
    opacity_scale: float = 1.0,
    progress_callback: Optional[Callable[[RecreateProgress], None]] = None,
) -> dict:
    """Main execution routine to build the ChartData directory and Plates.sqlite strictly from FAA data."""
    start_total_t = time.time()
    target_ext = ".png" if output_format == "png" else ".PDF"

    if not xml_path.is_file():
        raise FileNotFoundError(f"d-TPP XML metafile not found at: {xml_path}")
    if not faa_data_dir.is_dir():
        raise NotADirectoryError(f"FAA PDF data directory not found at: {faa_data_dir}")

    if progress_callback:
        progress_callback(
            RecreateProgress(
                stage="indexing",
                completed=0,
                total=1,
                speed_per_sec=0.0,
                elapsed_seconds=0.0,
                eta_seconds=0.0,
                message=f"Scanning FAA PDF source directories in {faa_data_dir.name}...",
            )
        )

    # Index available PDFs on disk and check page counts
    pdf_map, page_counts = index_source_pdfs(faa_data_dir)

    # Initialize pure FAA booklet indexer
    booklet_indexer = FAABookletIndex(pdf_map)

    # Optional reference database (only if user explicitly passes --reference-db)
    ref_airport_ids: Dict[str, int] = {}
    ref_georef: Dict[Tuple[str, str], dict] = {}
    ref_booklets: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    countries_rows: List[tuple] = []
    regions_rows: List[tuple] = []
    next_airport_id = 1

    if ref_db_path:
        ref_airport_ids, ref_georef, ref_booklets, countries_rows, regions_rows, max_aid = load_optional_reference_db(ref_db_path)
        if max_aid > 0:
            next_airport_id = max_aid + 1
    else:
        print("[*] Building standalone ChartData strictly from official FAA XML and source PDFs.")

    # Prepare output directories
    plates_dir = output_dir / "Plates"
    us_dir = plates_dir / "US"
    db_path = plates_dir / "Plates.sqlite"

    if clean and output_dir.exists():
        print(f"[*] Cleaning existing output directory: {output_dir} ...")
        shutil.rmtree(output_dir)

    us_dir.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    # Parse primary d-TPP XML
    if progress_callback:
        progress_callback(
            RecreateProgress(
                stage="parsing_xml",
                completed=0,
                total=1,
                speed_per_sec=0.0,
                elapsed_seconds=time.time() - start_total_t,
                eta_seconds=0.0,
                message=f"Parsing official FAA d-TPP Metafile ({xml_path.name})...",
            )
        )

    print(f"[*] Parsing official FAA d-TPP Metafile: {xml_path} ...")
    tree = ET.parse(xml_path)
    root = tree.getroot()

    cycle = str(root.attrib.get("cycle") or "2610").strip()
    from_edate_raw = str(root.attrib.get("from_edate") or "").strip()
    to_edate_raw = str(root.attrib.get("to_edate") or "").strip()
    start_date = parse_edate(from_edate_raw) or "2026-10-01"
    end_date = parse_edate(to_edate_raw) or "2026-10-29"

    print(f"[+] FAA d-TPP Cycle: {cycle} (Valid: {start_date} to {end_date})")

    # Connect to new SQLite database
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()

    # Database pragmas
    c.execute("PRAGMA page_size = 1024;")
    c.execute("PRAGMA encoding = 'UTF-8';")
    c.execute("PRAGMA synchronous = OFF;")
    c.execute("PRAGMA journal_mode = MEMORY;")

    # Create tables
    c.executescript("""
        CREATE TABLE Info (
            Version TEXT collate nocase,
            Copyright TEXT collate nocase,
            Start_Date TEXT collate nocase,
            End_Date TEXT collate nocase,
            LargeURLBase TEXT collate nocase,
            SmallURLBase TEXT collate nocase,
            CertificationLevel INTEGER,
            Region INTEGER
        );

        CREATE TABLE Airports (
            Airport_Name_ID TEXT collate nocase,
            Apt_Ident TEXT collate nocase,
            ICAO_Ident TEXT collate nocase,
            Region TEXT collate nocase,
            Country TEXT collate nocase,
            StateProvince TEXT collate nocase,
            Airports_Id INTEGER
        );

        CREATE TABLE Charts (
            Airport_Name_ID TEXT collate nocase,
            Chart_Code TEXT collate nocase,
            Chartseq TEXT collate nocase,
            Chart_Name TEXT collate nocase,
            File_Name TEXT collate nocase,
            civil TEXT collate nocase,
            faanfd15 TEXT collate nocase,
            faanfd18 TEXT collate nocase,
            copter TEXT collate nocase,
            Lat1 TEXT collate nocase,
            Long1 TEXT collate nocase,
            Lat2 TEXT collate nocase,
            Long2 TEXT collate nocase,
            X1 TEXT collate nocase,
            Y1 TEXT collate nocase,
            X2 TEXT collate nocase,
            Y2 TEXT collate nocase,
            Orientation TEXT collate nocase,
            Draw_Left TEXT collate nocase,
            Draw_Right TEXT collate nocase,
            Draw_Top TEXT collate nocase,
            Draw_Bottom TEXT collate nocase,
            ValidWithinX TEXT collate nocase,
            ValidWithinY TEXT collate nocase,
            ValidWithinRadius TEXT collate nocase,
            ExcludeAreas TEXT collate nocase,
            UserAction TEXT collate nocase,
            Invalid TEXT collate nocase,
            LastChanged TEXT collate nocase,
            StartDate TEXT collate nocase,
            EndDate TEXT collate nocase,
            Airports_Id INTEGER
        );

        CREATE TABLE `COUNTRIES` (
            `ID`,
            `CODE`,
            `NAME`,
            `COUNTRY_AERO`,
            `CONTINENT`
        );

        CREATE TABLE `REGIONS` (
            `ID`,
            `GLOBALCODE`,
            `LOCALCODE`,
            `NAME`,
            `CONTINENT`,
            `COUNTRY`
        );
    """)

    # Insert Info record
    c.execute(
        "INSERT INTO Info VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "1",
            f"FAA d-TPP {cycle}",
            start_date,
            end_date,
            "https://www.faa.gov",
            "https://www.faa.gov",
            0,
            1,
        ),
    )

    airports_to_insert: List[tuple] = []
    charts_to_insert: List[tuple] = []
    used_airport_ids: Set[int] = set()
    ident_to_id: Dict[str, int] = {}

    single_page_needed: Dict[str, Tuple[str, bool]] = {}
    multi_page_needed: Set[str] = set()

    total_xml_records = 0
    matched_georef_count = 0
    region_index = 1

    for state_el in root.findall("state_code"):
        state_code = str(state_el.attrib.get("ID") or "").strip().upper()
        state_fullname = str(state_el.attrib.get("state_fullname") or state_code).strip().title()

        # Build REGIONS row dynamically from XML
        if not ref_db_path:
            regions_rows.append(
                (
                    region_index,
                    f"US-{state_code}",
                    state_code,
                    state_fullname,
                    "NA",
                    "US",
                )
            )
            region_index += 1

        for city_el in state_el.findall("city_name"):
            city_name = str(city_el.attrib.get("ID") or "").strip()

            for apt_el in city_el.findall("airport_name"):
                apt_id = str(apt_el.attrib.get("apt_ident") or "").strip().upper()
                icao_id = str(apt_el.attrib.get("icao_ident") or "").strip().upper()
                ident = icao_id if icao_id else apt_id
                if not ident:
                    continue

                # Sequential or mapped Airports_Id
                airports_id: int
                if ident in ref_airport_ids:
                    airports_id = ref_airport_ids[ident]
                elif apt_id in ref_airport_ids:
                    airports_id = ref_airport_ids[apt_id]
                else:
                    airports_id = next_airport_id
                    next_airport_id += 1

                used_airport_ids.add(airports_id)
                ident_to_id[ident] = airports_id
                if apt_id:
                    ident_to_id[apt_id] = airports_id

                if state_code == "PR":
                    country, region = "PR", "US"
                elif state_code == "VI":
                    country, region = "VI", "US"
                elif state_code == "GU" or state_code == "XX":
                    country, region = "GU", "NA"
                else:
                    country, region = "US", "US"

                airports_to_insert.append(
                    (
                        str(airports_id),
                        ident,
                        ident,
                        region,
                        country,
                        state_code,
                        airports_id,
                    )
                )

                for rec_el in apt_el.findall("record"):
                    total_xml_records += 1
                    pdf_name = str(rec_el.findtext("pdf_name") or "").strip()
                    if not pdf_name:
                        continue

                    pdf_name_u = pdf_name.upper()
                    page_cnt = page_counts.get(pdf_name_u, 1)

                    raw_chart_code = str(rec_el.findtext("chart_code") or "").strip().upper()
                    if chart_code_style == "airmate":
                        chart_code = CHART_CODE_MAP.get(raw_chart_code, raw_chart_code)
                    else:
                        chart_code = raw_chart_code

                    chart_seq = str(rec_el.findtext("chartseq") or "").strip()
                    orig_chart_name = str(rec_el.findtext("chart_name") or "").strip()
                    civil = rec_el.findtext("civil") or None
                    faanfd18 = rec_el.findtext("faanfd18") or None
                    copter = rec_el.findtext("copter") or None
                    user_action = rec_el.findtext("useraction") or None
                    amdt_date_raw = rec_el.findtext("amdtdate") or ""
                    last_changed = parse_amdtdate(amdt_date_raw, fallback=start_date)

                    # Georeferencing: Prioritize native extraction from official FAA PDF (/VP dictionary)
                    src_pdf_path = pdf_map.get(pdf_name_u)
                    georef = None
                    if src_pdf_path:
                        georef = extract_georef_from_pdf(src_pdf_path)

                    # Fallback to optional reference DB (e.g. for airport diagrams without /VP)
                    if not georef and ref_georef:
                        georef = ref_georef.get((ident, orig_chart_name.upper())) or ref_georef.get((apt_id, orig_chart_name.upper()))

                    if georef:
                        matched_georef_count += 1
                        lat1, long1 = str(georef["Lat1"]), str(georef["Long1"])
                        lat2, long2 = str(georef["Lat2"]), str(georef["Long2"])
                        x1 = str(georef.get("X1") if georef.get("X1") is not None else "0.0")
                        y1 = str(georef.get("Y1") if georef.get("Y1") is not None else "0.0")
                        x2 = str(georef.get("X2") if georef.get("X2") is not None else "1.0")
                        y2 = str(georef.get("Y2") if georef.get("Y2") is not None else "1.0")
                        orient = str(georef.get("Orientation") if georef.get("Orientation") is not None else "0.0")
                        d_left = str(georef.get("Draw_Left") if georef.get("Draw_Left") is not None else "0.0")
                        d_right = str(georef.get("Draw_Right") if georef.get("Draw_Right") is not None else "1.0")
                        d_top = str(georef.get("Draw_Top") if georef.get("Draw_Top") is not None else "0.0")
                        d_bottom = str(georef.get("Draw_Bottom") if georef.get("Draw_Bottom") is not None else "1.0")
                        v_x, v_y = georef.get("ValidWithinX"), georef.get("ValidWithinY")
                        v_rad, exc_areas = georef.get("ValidWithinRadius"), georef.get("ExcludeAreas")
                    else:
                        lat1 = long1 = lat2 = long2 = None
                        x1 = y1 = x2 = y2 = None
                        orient = d_left = d_right = d_top = d_bottom = None
                        v_x = v_y = v_rad = exc_areas = None

                    stem, orig_ext = os.path.splitext(pdf_name)
                    chart_target_name = f"{stem}{target_ext}"
                    is_iac = chart_code in ("IAC", "IAP", "DAU")

                    # Handle Single-Page vs Multi-Page PDF
                    if page_cnt <= 1:
                        if pdf_name_u in single_page_needed:
                            curr_tname, curr_iac = single_page_needed[pdf_name_u]
                            single_page_needed[pdf_name_u] = (chart_target_name, curr_iac or is_iac)
                        else:
                            single_page_needed[pdf_name_u] = (chart_target_name, is_iac)

                        charts_to_insert.append(
                            (
                                str(airports_id),
                                chart_code,
                                chart_seq,
                                orig_chart_name,
                                chart_target_name,
                                civil,
                                None,
                                faanfd18,
                                copter,
                                lat1, long1, lat2, long2,
                                x1, y1, x2, y2,
                                orient, d_left, d_right, d_top, d_bottom,
                                v_x, v_y, v_rad, exc_areas,
                                user_action,
                                "false",
                                last_changed,
                                start_date,
                                end_date,
                                airports_id,
                            )
                        )
                    else:
                        # Multi-page PDF: split into single-page items
                        multi_page_needed.add(pdf_name)
                        stem_u = stem.upper()

                        # Check reference DB first if explicitly provided
                        ref_entries = ref_booklets.get((ident, stem_u)) or ref_booklets.get((apt_id, stem_u)) if ref_db_path else None

                        if ref_entries:
                            for ref_cname, ref_fname in ref_entries:
                                target_chart_name = re.sub(r"\.[pP][dD][fF]$|\.[pP][nN][gG]$", target_ext, ref_fname)
                                charts_to_insert.append(
                                    (
                                        str(airports_id),
                                        chart_code,
                                        chart_seq,
                                        ref_cname,
                                        target_chart_name,
                                        civil,
                                        None,
                                        faanfd18,
                                        copter,
                                        lat1, long1, lat2, long2,
                                        x1, y1, x2, y2,
                                        orient, d_left, d_right, d_top, d_bottom,
                                        v_x, v_y, v_rad, exc_areas,
                                        user_action,
                                        "false",
                                        last_changed,
                                        start_date,
                                        end_date,
                                        airports_id,
                                    )
                                )
                        else:
                            # 100% Standalone FAA Vector Text Matching
                            matched_p = booklet_indexer.find_airport_page(
                                pdf_name=pdf_name,
                                city=city_name,
                                state=state_code,
                                apt_ident=apt_id,
                                icao_ident=icao_id,
                            )
                            target_chart_name = format_page_filename(stem, matched_p, target_ext)
                            p_chart_name = f"{orig_chart_name} p.{matched_p}"
                            charts_to_insert.append(
                                (
                                    str(airports_id),
                                    chart_code,
                                    chart_seq,
                                    p_chart_name,
                                    target_chart_name,
                                    civil,
                                    None,
                                    faanfd18,
                                    copter,
                                    lat1, long1, lat2, long2,
                                    x1, y1, x2, y2,
                                    orient, d_left, d_right, d_top, d_bottom,
                                    v_x, v_y, v_rad, exc_areas,
                                    user_action,
                                    "false",
                                    last_changed,
                                    start_date,
                                    end_date,
                                    airports_id,
                                )
                            )

    print(f"[+] Parsed {len(airports_to_insert):,} d-TPP airports, {len(charts_to_insert):,} charts.")

    # Parse DCS AFD XML (if enabled)
    afd_charts_count = 0
    if include_afd:
        afd_xmls = sorted(faa_data_dir.glob("DCS_*/afd_*.xml"))
        if afd_xmls:
            afd_xml = afd_xmls[0]
            print(f"[*] Processing official FAA AFD Supplement XML: {afd_xml} ...")
            afd_tree = ET.parse(afd_xml)
            afd_root = afd_tree.getroot()

            for loc_el in afd_root.findall("location"):
                for apt_el in loc_el.findall("airport"):
                    apt_id = (apt_el.findtext("aptid") or "").strip().upper()
                    if not apt_id:
                        continue

                    if apt_id not in ident_to_id:
                        airports_id = next_airport_id
                        next_airport_id += 1

                        used_airport_ids.add(airports_id)
                        ident_to_id[apt_id] = airports_id

                        airports_to_insert.append(
                            (
                                str(airports_id),
                                apt_id,
                                apt_id,
                                "US",
                                "US",
                                "",
                                airports_id,
                            )
                        )
                    else:
                        airports_id = ident_to_id[apt_id]

                    for pdf_el in apt_el.findall(".//pdf"):
                        pdf_name = (pdf_el.text or "").strip()
                        if not pdf_name:
                            continue

                        pdf_name_u = pdf_name.upper()
                        page_cnt = page_counts.get(pdf_name_u, 1)
                        stem, _ = os.path.splitext(pdf_name)
                        target_afd_name = f"{stem}{target_ext}"

                        if page_cnt <= 1:
                            if pdf_name_u not in single_page_needed:
                                single_page_needed[pdf_name_u] = (target_afd_name, False)
                            afd_charts_count += 1
                            charts_to_insert.append(
                                (
                                    str(airports_id),
                                    "AFD",
                                    "70100",
                                    "AFD",
                                    target_afd_name,
                                    None, None, None, None,
                                    None, None, None, None,
                                    None, None, None, None,
                                    None, None, None, None, None,
                                    None, None, None, None,
                                    None,
                                    "false",
                                    "",
                                    start_date,
                                    end_date,
                                    airports_id,
                                )
                            )
                        else:
                            multi_page_needed.add(pdf_name)
                            for p in range(1, page_cnt + 1):
                                target_page_name = format_page_filename(stem, p, target_ext)
                                afd_charts_count += 1
                                charts_to_insert.append(
                                    (
                                        str(airports_id),
                                        "AFD",
                                        "70100",
                                        f"AFD p.{p}",
                                        target_page_name,
                                        None, None, None, None,
                                        None, None, None, None,
                                        None, None, None, None,
                                        None, None, None, None, None,
                                        None, None, None, None,
                                        None,
                                        "false",
                                        "",
                                        start_date,
                                        end_date,
                                        airports_id,
                                    )
                                )

            print(f"[+] Added {afd_charts_count:,} AFD supplement charts from DCS.")

    # Insert into database
    if progress_callback:
        progress_callback(
            RecreateProgress(
                stage="writing_db",
                completed=0,
                total=1,
                speed_per_sec=0.0,
                elapsed_seconds=time.time() - start_total_t,
                eta_seconds=0.0,
                message=f"Writing SQLite database ({len(airports_to_insert):,} airports, {len(charts_to_insert):,} charts)...",
            )
        )

    print("[*] Writing SQLite database records ...")
    c.executemany("INSERT INTO Airports VALUES (?, ?, ?, ?, ?, ?, ?)", airports_to_insert)
    c.executemany(
        """
        INSERT INTO Charts VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?
        )
        """,
        charts_to_insert,
    )

    if not countries_rows:
        countries_rows = DEFAULT_COUNTRIES
    c.executemany("INSERT INTO COUNTRIES VALUES (?, ?, ?, ?, ?)", countries_rows)
    c.executemany("INSERT INTO REGIONS VALUES (?, ?, ?, ?, ?, ?)", regions_rows)

    # Indices
    print("[*] Creating database indices and optimizing ...")
    c.executescript("""
        CREATE UNIQUE INDEX PrimaryKey ON Airports (airport_name_Id);
        CREATE UNIQUE INDEX apt_ident ON Airports (apt_ident);
        CREATE INDEX icao_ident ON Airports (icao_ident);
        CREATE INDEX airport_name_Id ON Charts (airport_name_Id);
        ANALYZE;
    """)

    conn.commit()
    conn.close()
    print(f"[+] Successfully wrote database: {db_path} ({db_path.stat().st_size:,} bytes)")

    # -------------------------------------------------------------
    # Place Chart Files into Plates/US/ (PNG or PDF)
    # -------------------------------------------------------------
    us_dir.mkdir(parents=True, exist_ok=True)
    t_link_start = time.time()
    linked_count = 0
    split_pages_count = 0
    missing_count = 0
    completed = 0
    errors = 0

    if output_format == "png":
        print(f"[*] Preparing Dynon-compatible PNG generation (Mode: {png_mode.upper()}, DPI: {dpi}, Workers: {workers}) ...")
        render_tasks = []

        # 1. Single-page chart tasks
        for pdf_u, (target_name, is_iac) in single_page_needed.items():
            src_path = pdf_map.get(pdf_u)
            if not src_path or not src_path.is_file():
                missing_count += 1
                continue
            dst_path = us_dir / target_name
            render_tasks.append((src_path, dst_path, 0, is_iac, png_mode == "enhanced", dpi, None))

        # 2. Multi-page booklet tasks
        for pdf_name in multi_page_needed:
            src_path = pdf_map.get(pdf_name.upper())
            if not src_path or not src_path.is_file():
                missing_count += 1
                continue
            stem, _ = os.path.splitext(pdf_name)
            num_pages = page_counts.get(pdf_name.upper(), 1)
            for page_idx in range(num_pages):
                page_num = page_idx + 1
                target_name = format_page_filename(stem, page_num, target_ext)
                dst_path = us_dir / target_name
                twodigit_name = f"{stem}{page_num:02d}{target_ext}"
                alias_path = us_dir / twodigit_name if twodigit_name != target_name else None
                render_tasks.append((src_path, dst_path, page_idx, False, False, dpi, alias_path))

        if limit and limit > 0:
            print(f"[*] Limiting PNG generation to first {limit} tasks (--limit {limit})")
            render_tasks = render_tasks[:limit]

        total_tasks = len(render_tasks)
        print(f"[*] Rendering {total_tasks:,} PNG plates using {workers} concurrent worker threads ...")

        def _worker_fn(task):
            src_p, dst_p, p_idx, is_iac, enh, d_dpi, alias_p = task
            ok = render_plate_to_png(
                src_p,
                dst_p,
                page_idx=p_idx,
                is_iac=is_iac,
                enhanced=enh,
                dpi=d_dpi,
                aircraft_categories=aircraft_categories,
                target_approaches=target_approaches,
                enabled_colors=enabled_colors,
                opacity_scale=opacity_scale,
            )
            if ok and alias_p and not alias_p.exists():
                try:
                    os.link(str(dst_p), str(alias_p))
                except OSError:
                    try:
                        shutil.copy2(dst_p, alias_p)
                    except Exception:
                        pass
            return ok, dst_p.name

        last_cb_time = 0.0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_worker_fn, task): task for task in render_tasks}
            for fut in as_completed(futures):
                res, dst_name = fut.result()
                if res:
                    completed += 1
                else:
                    errors += 1

                now = time.time()
                el = now - t_link_start
                sp = completed / el if el > 0 else 0
                eta = (total_tasks - completed) / sp if sp > 0 else 0

                if completed % 100 == 0 or completed == total_tasks:
                    print(
                        f"\r[*] Rendered {completed:,}/{total_tasks:,} PNGs ({completed/total_tasks*100:.1f}%) "
                        f"— {sp:.1f} pl/s — ETA {int(eta)}s",
                        end="",
                        flush=True,
                    )

                if progress_callback and (now - last_cb_time >= 0.12 or completed == total_tasks):
                    last_cb_time = now
                    progress_callback(
                        RecreateProgress(
                            stage="rendering",
                            completed=completed,
                            total=total_tasks,
                            speed_per_sec=sp,
                            elapsed_seconds=el,
                            eta_seconds=eta,
                            message=f"Rendered {dst_name}",
                            current_item=dst_name,
                            is_success=res,
                        )
                    )

        print()
        link_elapsed = time.time() - t_link_start
    else:
        # PDF format: Place genuine single-page PDFs via link/copy
        single_items = list(single_page_needed.items())
        multi_items = list(multi_page_needed)
        if limit and limit > 0:
            single_items = single_items[:limit]
            multi_items = multi_items[:max(0, limit - len(single_items))]

        total_pdf_tasks = len(single_items) + len(multi_items)
        pdf_done = 0
        last_cb_time = 0.0

        print(f"[*] Placing {len(single_items):,} single-page PDFs into {us_dir} via {link_mode} ...")
        for pdf_name_u, (target_name, _) in single_items:
            src_path = pdf_map.get(pdf_name_u)
            dst_path = us_dir / target_name

            if dst_path.exists():
                linked_count += 1
                pdf_done += 1
                continue

            if not src_path or not src_path.is_file():
                missing_count += 1
                pdf_done += 1
                continue

            try:
                if link_mode == "hardlink":
                    try:
                        os.link(src_path, dst_path)
                    except OSError:
                        shutil.copy2(src_path, dst_path)
                elif link_mode == "symlink":
                    os.symlink(src_path, dst_path)
                elif link_mode == "copy":
                    shutil.copy2(src_path, dst_path)
                linked_count += 1
            except Exception as e:
                print(f"[!] Error linking {src_path} -> {dst_path}: {e}")
                missing_count += 1

            pdf_done += 1
            now = time.time()
            if progress_callback and (now - last_cb_time >= 0.12 or pdf_done == total_pdf_tasks):
                last_cb_time = now
                el = now - t_link_start
                sp = pdf_done / el if el > 0 else 0
                eta = (total_pdf_tasks - pdf_done) / sp if sp > 0 else 0
                progress_callback(
                    RecreateProgress(
                        stage="linking",
                        completed=pdf_done,
                        total=total_pdf_tasks,
                        speed_per_sec=sp,
                        elapsed_seconds=el,
                        eta_seconds=eta,
                        message=f"Linked {target_name}",
                        current_item=target_name,
                        is_success=True,
                    )
                )

        # Split multi-page PDFs into strictly single-page PDFs
        print(f"[*] Splitting {len(multi_items):,} multi-page PDFs into single-page PDFs (e.g. EC1ALT01.PDF, EC1ALT02.PDF) ...")
        for pdf_name in multi_items:
            src_path = pdf_map.get(pdf_name.upper())
            if not src_path or not src_path.is_file():
                missing_count += 1
                pdf_done += 1
                continue

            try:
                doc = pymupdf.open(src_path)
                num_pages = len(doc)
                stem, ext = os.path.splitext(pdf_name)

                for page_idx in range(num_pages):
                    page_num = page_idx + 1
                    target_name = format_page_filename(stem, page_num, target_ext)
                    target_path = us_dir / target_name

                    if not target_path.exists():
                        page_doc = pymupdf.open()
                        page_doc.insert_pdf(doc, from_page=page_idx, to_page=page_idx)
                        page_doc.save(str(target_path), garbage=3, deflate=True)
                        page_doc.close()

                    twodigit_name = f"{stem}{page_num:02d}{target_ext}"
                    twodigit_path = us_dir / twodigit_name
                    if twodigit_name != target_name and not twodigit_path.exists():
                        try:
                            os.link(str(target_path), str(twodigit_path))
                        except OSError:
                            pass

                    split_pages_count += 1

                doc.close()
            except Exception as e:
                print(f"[!] Error splitting multi-page PDF {src_path}: {e}")

            pdf_done += 1
            now = time.time()
            if progress_callback and (now - last_cb_time >= 0.12 or pdf_done == total_pdf_tasks):
                last_cb_time = now
                el = now - t_link_start
                sp = pdf_done / el if el > 0 else 0
                eta = (total_pdf_tasks - pdf_done) / sp if sp > 0 else 0
                progress_callback(
                    RecreateProgress(
                        stage="linking",
                        completed=pdf_done,
                        total=total_pdf_tasks,
                        speed_per_sec=sp,
                        elapsed_seconds=el,
                        eta_seconds=eta,
                        message=f"Split booklet {pdf_name}",
                        current_item=pdf_name,
                        is_success=True,
                    )
                )

        link_elapsed = time.time() - t_link_start

    total_elapsed = time.time() - start_total_t

    print("\n" + "=" * 65)
    print("✅ RECREATION COMPLETE SUMMARY (100% PURE FAA SOURCE DATA)")
    print("=" * 65)
    print(f"📁 Output Directory:      {output_dir}")
    print(f"📦 Plates Database:       {db_path}")
    print(f"📄 Chart Files Directory: {us_dir}")
    print(f"🎨 Output Format:         {output_format.upper()} ({'Dynon Avionics SkyView compatible' if output_format == 'png' else 'FAA Vector PDF'})")
    if output_format == "png":
        print(f"✨ PNG Enhancement Mode:  {png_mode.upper()} (DPI: {dpi})")
        print(f"🖼️  Total PNGs Generated:  {completed:,} (Errors: {errors})")
    else:
        print(f"📑 Single-Page PDFs:      {linked_count:,}")
        print(f"✂️  Multi-Page Split Pages: {split_pages_count:,}")
    print(f"🛩️  Total Airports:        {len(airports_to_insert):,}")
    print(f"📊 Total Charts in DB:    {len(charts_to_insert):,} (d-TPP: {len(charts_to_insert)-afd_charts_count:,}, AFD: {afd_charts_count:,})")
    print(f"🌐 Georeferenced Charts:  {matched_georef_count:,} (extracted from PDF / georeferenced)")
    print(f"⚡ File Processing Time:  {link_elapsed:.2f}s")
    print(f"⏱️  Total Elapsed Time:    {total_elapsed:.2f}s")
    print("=" * 65 + "\n")

    summary = {
        "output_dir": output_dir,
        "db_path": db_path,
        "db_size": db_path.stat().st_size if db_path.exists() else 0,
        "us_dir": us_dir,
        "output_format": output_format,
        "png_mode": png_mode,
        "dpi": dpi,
        "cycle": cycle,
        "valid_dates": (start_date, end_date),
        "total_airports": len(airports_to_insert),
        "total_charts": len(charts_to_insert),
        "georeferenced_charts": matched_georef_count,
        "afd_charts_count": afd_charts_count,
        "completed_files": completed if output_format == "png" else (linked_count + split_pages_count),
        "errors": errors if output_format == "png" else missing_count,
        "file_time": link_elapsed,
        "total_time": total_elapsed,
    }

    if progress_callback:
        progress_callback(
            RecreateProgress(
                stage="complete",
                completed=completed if output_format == "png" else total_pdf_tasks,
                total=total_tasks if output_format == "png" else total_pdf_tasks,
                speed_per_sec=(completed / link_elapsed) if (output_format == "png" and link_elapsed > 0) else 0.0,
                elapsed_seconds=total_elapsed,
                eta_seconds=0.0,
                message="Recreation complete!",
                is_success=True,
            )
        )

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recreate Dynon-compatible ChartData folder and Plates.sqlite strictly from official FAA data."
    )
    parser.add_argument(
        "--format",
        choices=["png", "pdf"],
        default="png",
        help="Output plate file format: 'png' (default, required for Dynon Avionics SkyView) or 'pdf'",
    )
    parser.add_argument(
        "--png-mode",
        choices=["enhanced", "standard", "non-enhanced"],
        default="enhanced",
        help="PNG rendering mode: 'enhanced' (applies 4-color tactical briefing to IAC approach plates, default) or 'standard' / 'non-enhanced'",
    )
    parser.add_argument(
        "--enhanced",
        action="store_true",
        help="Convenience flag: generate enhanced PNGs (same as --png-mode enhanced)",
    )
    parser.add_argument(
        "--non-enhanced",
        action="store_true",
        help="Convenience flag: generate non-enhanced standard PNGs (same as --png-mode standard)",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=200,
        help="Resolution in DPI for PNG rendering (default: 200, matching Dynon SkyView screen format)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of concurrent worker threads for PNG rendering (default: 8)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit on total charts to render (useful for quick validation)",
    )
    parser.add_argument(
        "--xml",
        type=Path,
        default=DEFAULT_XML_PATH,
        help=f"Path to official FAA d-TPP_Metafile.xml (default: {DEFAULT_XML_PATH})",
    )
    parser.add_argument(
        "--faa-data-dir",
        type=Path,
        default=DEFAULT_FAA_DATA_DIR,
        help=f"Path to source FAA PDF directory containing DDTPPA..E and DCS folders (default: {DEFAULT_FAA_DATA_DIR})",
    )
    parser.add_argument(
        "--reference-db",
        type=Path,
        default=None,
        help="Optional external reference database. Default is None (100%% pure FAA data).",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output root folder for ChartData (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--link-mode",
        choices=["hardlink", "copy", "symlink"],
        default="hardlink",
        help="When --format pdf is selected: 'hardlink' (instant, 0 extra disk space, default), 'copy', or 'symlink'",
    )
    parser.add_argument(
        "--no-afd",
        action="store_true",
        help="Do not include Airport/Facility Directory (AFD) supplement PDFs from DCS folders",
    )
    parser.add_argument(
        "--chart-code-style",
        choices=["airmate", "faa"],
        default="airmate",
        help="Chart code convention: 'airmate' (IAP->IAC, STR->STAR, ODP->DPO, default) or 'faa' (raw d-TPP codes)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Clean/delete existing output directory before generating",
    )

    args = parser.parse_args()

    ref_db = args.reference_db if (args.reference_db and args.reference_db.is_file()) else None

    # Resolve png_mode
    png_mode = "standard" if args.non_enhanced else ("enhanced" if args.enhanced else args.png_mode)
    if png_mode == "non-enhanced":
        png_mode = "standard"

    build_chartdata(
        xml_path=args.xml,
        faa_data_dir=args.faa_data_dir,
        output_dir=args.output_dir,
        ref_db_path=ref_db,
        output_format=args.format,
        png_mode=png_mode,
        dpi=args.dpi,
        workers=args.workers,
        link_mode=args.link_mode,
        include_afd=not args.no_afd,
        chart_code_style=args.chart_code_style,
        clean=args.clean,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
