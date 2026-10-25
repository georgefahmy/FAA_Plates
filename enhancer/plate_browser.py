"""
FAA d-TPP XML-Backed SQLite Plate Database & File Browser (`enhancer/plate_browser.py`).

Parses the official FAA Digital Terminal Procedures Publication (`d-TPP_Metafile.xml`)
and indexes all `.PDF` chart files inside `/Users/GFahmy/Documents/RV-7/Software/sv_software/FAA_pdf_data`
(across `DDTPPA_*`, `DDTPPB_*`, `DDTPPC_*`, `DDTPPD_*`, `DDTPPE_*`) into a fast cached
SQLite database so pilots can search and browse plates by:
- State / Territory (e.g., `CA - California`)
- City (e.g., `SAN MARTIN`, `SAN JOSE`, `SANTA MONICA`)
- Airport Identifier or Name (e.g., `E16`, `KSJC`, `SJC`, `KSMO`, `SAN MARTIN`)
- Procedure / Chart Name (e.g., `RNAV (GPS) RWY 32`, `ILS OR LOC`)
- Chart Type (`IAP` Instrument Approach Procedures, `APD` Airport Diagrams, `DP`, `STR`, `MIN`, `ODP`, `HOT`)
"""

from __future__ import annotations

import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_FAA_PDF_DIR = Path("/Users/GFahmy/Documents/RV-7/Software/sv_software/FAA_pdf_data")
DEFAULT_PLATES_DIR = DEFAULT_FAA_PDF_DIR  # Backwards-compatible alias

CHART_CODE_LABELS: Dict[str, str] = {
    "IAP": "IAP — Instrument Approach Procedures",
    "APD": "APD — Airport Diagrams",
    "DP": "DP — Departure Procedures (SIDs)",
    "STR": "STR — Standard Terminal Arrivals (STARs)",
    "MIN": "MIN — Takeoff & Alternate Minimums",
    "ODP": "ODP — Obstacle Departure Procedures",
    "HOT": "HOT — Airport Hot Spots",
    "LAH": "LAH — Land and Hold Short",
    "ALL": "ALL — All Chart Types",
}


@dataclass(frozen=True)
class AirportEntry:
    """Airport record parsed from `d-TPP_Metafile.xml`."""
    airport_name_id: str
    apt_ident: str
    icao_ident: str
    state_code: str
    state_name: str
    country: str
    region: str
    city: str
    airport_name: str
    iac_count: int
    total_chart_count: int

    @property
    def display_label(self) -> str:
        code = (
            f"{self.icao_ident} ({self.apt_ident})"
            if self.icao_ident and self.icao_ident != self.apt_ident
            else (self.icao_ident or self.apt_ident)
        )
        loc_parts = [p.title() if p.isupper() else p for p in (self.city, self.state_code) if p]
        loc_str = ", ".join(loc_parts)
        apt_title = self.airport_name.title() if self.airport_name.isupper() else self.airport_name
        if apt_title and loc_str:
            return f"{code} — {apt_title} ({loc_str}) [{self.iac_count} IAPs]"
        if loc_str:
            return f"{code} — {loc_str} [{self.iac_count} IAPs]"
        return f"{code} [{self.iac_count} IAPs]"


@dataclass(frozen=True)
class PlateChartEntry:
    """Single PDF chart/plate record from `d-TPP_Metafile.xml`."""
    airport_name_id: str
    apt_ident: str
    icao_ident: str
    chart_code: str
    chart_name: str
    file_name: str
    region: str
    file_path: Path
    city: str = ""
    state_code: str = ""
    airport_name: str = ""
    amdt_num: str = ""
    amdt_date: str = ""
    proc_uid: str = ""

    @property
    def exists(self) -> bool:
        return self.file_path.is_file()

    @property
    def display_label(self) -> str:
        return f"[{self.chart_code}] {self.chart_name} ({self.file_name})"


class DTPPPlateBrowser:
    """
    Fast XML-backed SQLite browser for the consolidated FAA d-TPP PDF directory
    (`/Users/GFahmy/Documents/RV-7/Software/sv_software/FAA_pdf_data`).
    """

    def __init__(
        self,
        data_dir: Path | str = DEFAULT_FAA_PDF_DIR,
        cache_db_path: Optional[Path | str] = None,
    ):
        self.data_dir = Path(data_dir)
        self.plates_dir = self.data_dir
        self.xml_path = self._find_metafile_xml()
        if cache_db_path is not None:
            self.db_path = Path(cache_db_path)
        else:
            cache_dir = Path(__file__).resolve().parent.parent / ".cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = cache_dir / "dtpp_catalog.sqlite"

        self.cycle: str = ""
        self.from_edate: str = ""
        self.to_edate: str = ""
        self._airports: List[AirportEntry] = []
        self._by_id: Dict[str, AirportEntry] = {}

        if self.is_available:
            self._ensure_sqlite_index()
            self._load_airports_index()

    def _find_metafile_xml(self) -> Optional[Path]:
        if not self.data_dir.is_dir():
            return None
        direct = self.data_dir / "d-TPP_Metafile.xml"
        if direct.is_file():
            return direct
        matches = sorted(
            self.data_dir.rglob("d-TPP_Metafile.xml"),
            key=lambda p: (str(p.parent.name), p.stat().st_mtime),
            reverse=True,
        )
        if matches:
            return matches[0]
        xmls = sorted(self.data_dir.rglob("*.xml"), key=lambda p: p.stat().st_mtime, reverse=True)
        return xmls[0] if xmls else None

    def refresh(self) -> bool:
        """Force re-scan of data_dir for d-TPP_Metafile.xml and reload index if found."""
        new_xml = self._find_metafile_xml()
        if new_xml and new_xml.is_file():
            self.xml_path = new_xml
            self._ensure_sqlite_index()
            self._load_airports_index()
            return True
        return False

    @property
    def is_available(self) -> bool:
        if self.xml_path is None or not self.xml_path.is_file() or not self._airports:
            self.refresh()
        return self.xml_path is not None and self.xml_path.is_file() and len(self._airports) > 0

    def _ensure_sqlite_index(self) -> None:
        """
        Build or refresh the cached SQLite catalog (`dtpp_catalog.sqlite`) from
        `d-TPP_Metafile.xml` and the PDF files inside `self.data_dir`.
        """
        assert self.xml_path is not None
        xml_mtime = int(self.xml_path.stat().st_mtime)

        if self.db_path.is_file():
            try:
                conn = sqlite3.connect(str(self.db_path))
                cur = conn.cursor()
                cur.execute("SELECT key, value FROM meta")
                meta = dict(cur.fetchall())
                conn.close()
                if (
                    meta.get("xml_mtime") == str(xml_mtime)
                    and meta.get("data_dir") == str(self.data_dir)
                ):
                    self.cycle = meta.get("cycle", "")
                    self.from_edate = meta.get("from_edate", "")
                    self.to_edate = meta.get("to_edate", "")
                    return
            except Exception:
                pass

        # Index all PDF files on disk (excluding comparison subfolders)
        pdf_map: Dict[str, str] = {}
        for p in self.data_dir.rglob("*.[pP][dD][fF]"):
            if "compare_pdf" not in p.parts:
                pdf_map[p.name.upper()] = str(p)

        tree = ET.parse(self.xml_path)
        root = tree.getroot()
        self.cycle = str(root.attrib.get("cycle") or "").strip()
        self.from_edate = str(root.attrib.get("from_edate") or "").strip()
        self.to_edate = str(root.attrib.get("to_edate") or "").strip()

        if self.db_path.is_file():
            self.db_path.unlink()

        conn = sqlite3.connect(str(self.db_path))
        try:
            cur = conn.cursor()
            cur.executescript(
                """
                CREATE TABLE meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE airports (
                    airport_id TEXT PRIMARY KEY,
                    apt_ident TEXT,
                    icao_ident TEXT,
                    state_code TEXT,
                    state_name TEXT,
                    volume TEXT,
                    city TEXT,
                    airport_name TEXT,
                    military TEXT,
                    iap_count INTEGER,
                    total_chart_count INTEGER
                );
                CREATE TABLE charts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    airport_id TEXT,
                    apt_ident TEXT,
                    icao_ident TEXT,
                    state_code TEXT,
                    city TEXT,
                    airport_name TEXT,
                    volume TEXT,
                    chart_seq INTEGER,
                    chart_code TEXT,
                    chart_name TEXT,
                    pdf_name TEXT,
                    pdf_path TEXT,
                    amdt_num TEXT,
                    amdt_date TEXT,
                    proc_uid TEXT
                );
                CREATE INDEX idx_charts_airport_id ON charts(airport_id);
                CREATE INDEX idx_charts_code ON charts(chart_code);
                CREATE INDEX idx_charts_name ON charts(chart_name);
                CREATE INDEX idx_airports_state ON airports(state_code);
                CREATE INDEX idx_airports_ident ON airports(apt_ident, icao_ident);
                """
            )

            cur.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                [
                    ("xml_mtime", str(xml_mtime)),
                    ("data_dir", str(self.data_dir)),
                    ("cycle", self.cycle),
                    ("from_edate", self.from_edate),
                    ("to_edate", self.to_edate),
                ],
            )

            airport_rows = []
            chart_rows = []

            for state_el in root.findall("state_code"):
                state_code = str(state_el.attrib.get("ID") or "").strip().upper()
                state_fullname = str(state_el.attrib.get("state_fullname") or state_code).strip().title()

                for city_el in state_el.findall("city_name"):
                    city_name = str(city_el.attrib.get("ID") or "").strip()
                    volume = str(city_el.attrib.get("volume") or "").strip()

                    for apt_el in city_el.findall("airport_name"):
                        apt_name = str(apt_el.attrib.get("ID") or "").strip()
                        apt_ident = str(apt_el.attrib.get("apt_ident") or "").strip().upper()
                        icao_ident = str(apt_el.attrib.get("icao_ident") or "").strip().upper()
                        alnum = str(apt_el.attrib.get("alnum") or "").strip()
                        military = str(apt_el.attrib.get("military") or "N").strip()
                        airport_id = f"{state_code}_{apt_ident}_{alnum}" if alnum else f"{state_code}_{apt_ident}_{city_name}"

                        iap_cnt = 0
                        total_cnt = 0

                        for rec_el in apt_el.findall("record"):
                            pdf_name = str(rec_el.findtext("pdf_name") or "").strip()
                            if not pdf_name:
                                continue
                            pdf_path = pdf_map.get(pdf_name.upper(), "")
                            if not pdf_path:
                                continue

                            chart_code = str(rec_el.findtext("chart_code") or "").strip().upper()
                            chart_name = str(rec_el.findtext("chart_name") or "").strip()
                            chart_seq_str = str(rec_el.findtext("chartseq") or "99999").strip()
                            chart_seq = int(chart_seq_str) if chart_seq_str.isdigit() else 99999
                            amdt_num = str(rec_el.findtext("amdtnum") or "").strip()
                            amdt_date = str(rec_el.findtext("amdtdate") or "").strip()
                            proc_uid = str(rec_el.findtext("procuid") or "").strip()

                            if chart_code == "IAP":
                                iap_cnt += 1
                            total_cnt += 1

                            chart_rows.append(
                                (
                                    airport_id,
                                    apt_ident,
                                    icao_ident,
                                    state_code,
                                    city_name,
                                    apt_name,
                                    volume,
                                    chart_seq,
                                    chart_code,
                                    chart_name,
                                    pdf_name,
                                    pdf_path,
                                    amdt_num,
                                    amdt_date,
                                    proc_uid,
                                )
                            )

                        if total_cnt > 0:
                            airport_rows.append(
                                (
                                    airport_id,
                                    apt_ident,
                                    icao_ident,
                                    state_code,
                                    state_fullname,
                                    volume,
                                    city_name,
                                    apt_name,
                                    military,
                                    iap_cnt,
                                    total_cnt,
                                )
                            )

            cur.executemany(
                """
                INSERT OR REPLACE INTO airports (
                    airport_id, apt_ident, icao_ident, state_code, state_name,
                    volume, city, airport_name, military, iap_count, total_chart_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                airport_rows,
            )
            cur.executemany(
                """
                INSERT INTO charts (
                    airport_id, apt_ident, icao_ident, state_code, city, airport_name,
                    volume, chart_seq, chart_code, chart_name, pdf_name, pdf_path,
                    amdt_num, amdt_date, proc_uid
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                chart_rows,
            )
            conn.commit()
        finally:
            conn.close()

    def _load_airports_index(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT
                    airport_id, apt_ident, icao_ident, state_code, state_name,
                    volume, city, airport_name, iap_count, total_chart_count
                FROM airports
                ORDER BY state_code, city, apt_ident
                """
            )
            rows = cur.fetchall()
        finally:
            conn.close()

        airports: List[AirportEntry] = []
        for r in rows:
            entry = AirportEntry(
                airport_name_id=r[0],
                apt_ident=r[1],
                icao_ident=r[2],
                state_code=r[3],
                state_name=r[4],
                country="US",
                region=r[5],
                city=r[6],
                airport_name=r[7],
                iac_count=int(r[8]),
                total_chart_count=int(r[9]),
            )
            airports.append(entry)
            self._by_id[entry.airport_name_id] = entry

        self._airports = airports

    def list_states(self, country: str = "US", only_with_iac: bool = True) -> List[Tuple[str, str, int]]:
        """Return sorted list of `(state_code, state_name, airport_count)`."""
        counts: Dict[str, int] = {}
        names: Dict[str, str] = {}
        for a in self._airports:
            if only_with_iac and a.iac_count == 0:
                continue
            if not a.state_code:
                continue
            counts[a.state_code] = counts.get(a.state_code, 0) + 1
            names[a.state_code] = a.state_name

        return sorted(
            [(sc, names[sc], cnt) for sc, cnt in counts.items()],
            key=lambda item: (item[1], item[0]),
        )

    def list_cities(
        self,
        state_code: Optional[str] = None,
        country: str = "US",
        only_with_iac: bool = True,
    ) -> List[str]:
        """Return sorted unique city names for the selected state."""
        cities = set()
        for a in self._airports:
            if state_code and a.state_code != state_code.upper():
                continue
            if only_with_iac and a.iac_count == 0:
                continue
            if a.city:
                cities.add(a.city)
        return sorted(cities)

    def filter_airports(
        self,
        state_code: Optional[str] = None,
        city: Optional[str] = None,
        search_query: Optional[str] = None,
        country: str = "US",
        only_with_iac: bool = True,
    ) -> List[AirportEntry]:
        """
        Filter airports by State, City, and/or free-text query (matching ICAO,
        FAA LID, city name, airport name, or state).
        """
        q = (search_query or "").strip().lower()
        city_norm = (city or "").strip().lower()
        results: List[AirportEntry] = []

        for a in self._airports:
            if only_with_iac and a.iac_count == 0:
                continue
            if state_code and a.state_code != state_code.upper():
                continue
            if city_norm and a.city.lower() != city_norm:
                continue
            if q:
                haystack = (
                    f"{a.icao_ident} {a.apt_ident} k{a.apt_ident} "
                    f"{a.city} {a.airport_name} {a.state_code} {a.state_name}"
                ).lower()
                if not all(tok in haystack for tok in q.split()):
                    continue
            results.append(a)

        if q:
            def _rank(a: AirportEntry) -> Tuple[int, str, str]:
                q_up = q.upper()
                if a.icao_ident == q_up or a.apt_ident == q_up or f"K{a.apt_ident}" == q_up:
                    return (0, a.state_code, a.apt_ident)
                if a.icao_ident.startswith(q_up) or a.apt_ident.startswith(q_up):
                    return (1, a.state_code, a.apt_ident)
                if a.city.lower().startswith(q):
                    return (2, a.state_code, a.apt_ident)
                return (3, a.state_code, a.apt_ident)

            results.sort(key=_rank)

        return results

    def get_charts_for_airport(
        self,
        airport_name_id: str,
        chart_codes: Optional[List[str]] = None,
    ) -> List[PlateChartEntry]:
        """
        Return all PDF charts for `airport_name_id` (accepts `'IAP'` or `'IAC'`).
        """
        normalized_codes: Optional[List[str]] = None
        if chart_codes:
            normalized_codes = []
            for c in chart_codes:
                cu = c.upper()
                if cu in ("IAC", "IAP"):
                    normalized_codes.extend(["IAP", "IAC"])
                elif cu in ("STAR", "STR"):
                    normalized_codes.extend(["STR", "STAR"])
                elif cu != "ALL":
                    normalized_codes.append(cu)

        conn = sqlite3.connect(str(self.db_path))
        try:
            cur = conn.cursor()
            if normalized_codes:
                placeholders = ",".join("?" for _ in normalized_codes)
                cur.execute(
                    f"""
                    SELECT
                        airport_id, apt_ident, icao_ident, chart_code, chart_name,
                        pdf_name, volume, pdf_path, city, state_code, airport_name,
                        amdt_num, amdt_date, proc_uid
                    FROM charts
                    WHERE airport_id = ?
                      AND chart_code IN ({placeholders})
                    ORDER BY chart_seq, chart_name
                    """,
                    [airport_name_id, *normalized_codes],
                )
            else:
                cur.execute(
                    """
                    SELECT
                        airport_id, apt_ident, icao_ident, chart_code, chart_name,
                        pdf_name, volume, pdf_path, city, state_code, airport_name,
                        amdt_num, amdt_date, proc_uid
                    FROM charts
                    WHERE airport_id = ?
                    ORDER BY chart_seq, chart_name
                    """,
                    [airport_name_id],
                )
            rows = cur.fetchall()
        finally:
            conn.close()

        charts: List[PlateChartEntry] = []
        for r in rows:
            entry = PlateChartEntry(
                airport_name_id=r[0],
                apt_ident=r[1],
                icao_ident=r[2],
                chart_code=r[3],
                chart_name=r[4],
                file_name=r[5],
                region=r[6],
                file_path=Path(r[7]),
                city=r[8],
                state_code=r[9],
                airport_name=r[10],
                amdt_num=r[11],
                amdt_date=r[12],
                proc_uid=r[13],
            )
            if entry.exists:
                charts.append(entry)
        return charts

    def search_plates_directly(
        self,
        query: str,
        chart_code: str = "IAP",
        limit: int = 50,
    ) -> List[PlateChartEntry]:
        """
        Search plates directly across all airports by airport code, city, airport name,
        or chart name (e.g., `'E16 RNAV 32'`, `'KSJC ILS 30L'`, `'San Martin'`).
        """
        tokens = [t.strip() for t in query.strip().split() if t.strip()]
        if not tokens:
            return []

        conn = sqlite3.connect(str(self.db_path))
        try:
            cur = conn.cursor()
            where_clauses = []
            params: List[object] = []
            if chart_code and chart_code.upper() not in ("ALL", ""):
                cc = "IAP" if chart_code.upper() in ("IAP", "IAC") else chart_code.upper()
                where_clauses.append("chart_code = ?")
                params.append(cc)

            for tok in tokens:
                like_tok = f"%{tok}%"
                # Also match stripped leading 'K' for 4-letter US ICAO codes
                alt_tok = tok[1:] if len(tok) == 4 and tok.upper().startswith("K") else tok
                where_clauses.append(
                    "(apt_ident LIKE ? OR icao_ident LIKE ? OR city LIKE ? "
                    "OR airport_name LIKE ? OR chart_name LIKE ? OR pdf_name LIKE ?)"
                )
                params.extend([f"%{alt_tok}%", like_tok, like_tok, like_tok, like_tok, like_tok])

            where_sql = " AND ".join(where_clauses)
            cur.execute(
                f"""
                SELECT
                    airport_id, apt_ident, icao_ident, chart_code, chart_name,
                    pdf_name, volume, pdf_path, city, state_code, airport_name,
                    amdt_num, amdt_date, proc_uid
                FROM charts
                WHERE {where_sql}
                ORDER BY state_code, apt_ident, chart_seq, chart_name
                LIMIT ?
                """,
                [*params, limit],
            )
            rows = cur.fetchall()
        finally:
            conn.close()

        return [
            PlateChartEntry(
                airport_name_id=r[0],
                apt_ident=r[1],
                icao_ident=r[2],
                chart_code=r[3],
                chart_name=r[4],
                file_name=r[5],
                region=r[6],
                file_path=Path(r[7]),
                city=r[8],
                state_code=r[9],
                airport_name=r[10],
                amdt_num=r[11],
                amdt_date=r[12],
                proc_uid=r[13],
            )
            for r in rows
            if Path(r[7]).is_file()
        ]


# Backwards-compatible alias
AirmatePlateBrowser = DTPPPlateBrowser
