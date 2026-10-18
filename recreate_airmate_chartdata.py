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
import os
import re
import shutil
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf
    except ImportError:
        print("[!] Error: PyMuPDF ('pymupdf' or 'fitz') is required. Install via: pip install pymupdf")
        sys.exit(1)


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


def format_page_filename(stem: str, page_num: int, ext: str = ".PDF") -> str:
    """
    Format single-page filename according to standard 01, 02 sequencing:
    e.g. EC1ALT + 1 -> EC1ALT01.PDF
         EC1ALT + 5 -> EC1ALT05.PDF
         SW2TO + 29 -> SW2TO029.PDF
         AKTO + 10 -> AKTO010.PDF
    """
    return f"{stem}0{page_num}{ext}"


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
    link_mode: str = "hardlink",
    include_afd: bool = True,
    chart_code_style: str = "airmate",
    clean: bool = False,
) -> None:
    """Main execution routine to build the ChartData directory and Plates.sqlite strictly from FAA data."""
    start_total_t = time.time()

    if not xml_path.is_file():
        raise FileNotFoundError(f"d-TPP XML metafile not found at: {xml_path}")
    if not faa_data_dir.is_dir():
        raise NotADirectoryError(f"FAA PDF data directory not found at: {faa_data_dir}")

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

    single_page_needed: Set[str] = set()
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

                    # Georeferencing (if reference DB was provided)
                    georef = ref_georef.get((ident, orig_chart_name.upper())) or ref_georef.get((apt_id, orig_chart_name.upper()))
                    if georef:
                        matched_georef_count += 1
                        lat1, long1 = georef["Lat1"], georef["Long1"]
                        lat2, long2 = georef["Lat2"], georef["Long2"]
                        x1, y1 = georef["X1"], georef["Y1"]
                        x2, y2 = georef["X2"], georef["Y2"]
                        orient = georef["Orientation"]
                        d_left, d_right = georef["Draw_Left"], georef["Draw_Right"]
                        d_top, d_bottom = georef["Draw_Top"], georef["Draw_Bottom"]
                        v_x, v_y = georef["ValidWithinX"], georef["ValidWithinY"]
                        v_rad, exc_areas = georef["ValidWithinRadius"], georef["ExcludeAreas"]
                    else:
                        lat1 = long1 = lat2 = long2 = None
                        x1 = y1 = x2 = y2 = None
                        orient = d_left = d_right = d_top = d_bottom = None
                        v_x = v_y = v_rad = exc_areas = None

                    # Handle Single-Page vs Multi-Page PDF
                    if page_cnt <= 1:
                        single_page_needed.add(pdf_name)
                        charts_to_insert.append(
                            (
                                str(airports_id),
                                chart_code,
                                chart_seq,
                                orig_chart_name,
                                pdf_name,
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
                        stem, ext = os.path.splitext(pdf_name)
                        stem_u = stem.upper()

                        # Check reference DB first if explicitly provided
                        ref_entries = ref_booklets.get((ident, stem_u)) or ref_booklets.get((apt_id, stem_u)) if ref_db_path else None

                        if ref_entries:
                            for ref_cname, ref_fname in ref_entries:
                                target_pdf_name = re.sub(r"\.[pP][nN][gG]$", ext, ref_fname)
                                charts_to_insert.append(
                                    (
                                        str(airports_id),
                                        chart_code,
                                        chart_seq,
                                        ref_cname,
                                        target_pdf_name,
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
                            target_pdf_name = format_page_filename(stem, matched_p, ext)
                            p_chart_name = f"{orig_chart_name} p.{matched_p}"
                            charts_to_insert.append(
                                (
                                    str(airports_id),
                                    chart_code,
                                    chart_seq,
                                    p_chart_name,
                                    target_pdf_name,
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

                        if page_cnt <= 1:
                            single_page_needed.add(pdf_name)
                            afd_charts_count += 1
                            charts_to_insert.append(
                                (
                                    str(airports_id),
                                    "AFD",
                                    "70100",
                                    "AFD",
                                    pdf_name,
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
                            stem, ext = os.path.splitext(pdf_name)
                            for p in range(1, page_cnt + 1):
                                target_pdf_name = format_page_filename(stem, p, ext)
                                afd_charts_count += 1
                                charts_to_insert.append(
                                    (
                                        str(airports_id),
                                        "AFD",
                                        "70100",
                                        f"AFD p.{p}",
                                        target_pdf_name,
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
    # Place Single-Page PDFs into Plates/US/
    # -------------------------------------------------------------
    us_dir.mkdir(parents=True, exist_ok=True)
    t_link_start = time.time()
    linked_count = 0
    split_pages_count = 0
    missing_count = 0

    # 1. Place genuine single-page PDFs via link/copy
    print(f"[*] Placing {len(single_page_needed):,} single-page PDFs into {us_dir} via {link_mode} ...")
    for pdf_name in single_page_needed:
        src_path = pdf_map.get(pdf_name.upper())
        dst_path = us_dir / pdf_name

        if dst_path.exists():
            linked_count += 1
            continue

        if not src_path or not src_path.is_file():
            missing_count += 1
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

    # 2. Split multi-page PDFs into strictly single-page PDFs
    print(f"[*] Splitting {len(multi_page_needed):,} multi-page PDFs into single-page PDFs (e.g. EC1ALT01.PDF, EC1ALT02.PDF) ...")
    for pdf_name in multi_page_needed:
        src_path = pdf_map.get(pdf_name.upper())
        if not src_path or not src_path.is_file():
            missing_count += 1
            continue

        try:
            doc = pymupdf.open(src_path)
            num_pages = len(doc)
            stem, ext = os.path.splitext(pdf_name)

            for page_idx in range(num_pages):
                page_num = page_idx + 1
                target_name = format_page_filename(stem, page_num, ext)
                target_path = us_dir / target_name

                if not target_path.exists():
                    page_doc = pymupdf.open()
                    page_doc.insert_pdf(doc, from_page=page_idx, to_page=page_idx)
                    page_doc.save(str(target_path), garbage=3, deflate=True)
                    page_doc.close()

                twodigit_name = f"{stem}{page_num:02d}{ext}"
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

    link_elapsed = time.time() - t_link_start
    total_elapsed = time.time() - start_total_t

    print("\n" + "=" * 65)
    print("✅ RECREATION COMPLETE SUMMARY (100% PURE FAA SOURCE DATA)")
    print("=" * 65)
    print(f"📁 Output Directory:      {output_dir}")
    print(f"📦 Plates Database:       {db_path}")
    print(f"📄 Target PDF Directory:   {us_dir}")
    print(f"🛩️  Total Airports:        {len(airports_to_insert):,}")
    print(f"📊 Total Charts:          {len(charts_to_insert):,} (d-TPP: {len(charts_to_insert)-afd_charts_count:,}, AFD: {afd_charts_count:,})")
    print(f"📑 Single-Page PDFs Linked: {linked_count:,}")
    print(f"✂️  Multi-Page Split Pages: {split_pages_count:,} from {len(multi_page_needed):,} multi-page files")
    print(f"⚡ Processing Time:       {link_elapsed:.2f}s")
    print(f"⏱️  Total Elapsed Time:    {total_elapsed:.2f}s")
    print("=" * 65 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recreate ChartData folder and Plates.sqlite strictly from official FAA PDFs and XML metadata."
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
        help="How to place PDFs in Plates/US/: 'hardlink' (instant, 0 extra disk space, default), 'copy' (independent files), 'symlink' (symbolic links)",
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

    build_chartdata(
        xml_path=args.xml,
        faa_data_dir=args.faa_data_dir,
        output_dir=args.output_dir,
        ref_db_path=ref_db,
        link_mode=args.link_mode,
        include_afd=not args.no_afd,
        chart_code_style=args.chart_code_style,
        clean=args.clean,
    )


if __name__ == "__main__":
    main()
