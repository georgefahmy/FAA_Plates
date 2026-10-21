"""
Batch Enhancement Engine for FAA Instrument Approach Procedure (IAC) Plates.

Discovers and batch-processes all IAC / IAP plates across a user-specified
directory (such as an Airmate-compatible `ChartData` folder or FAA data tree),
applying deterministic 4-Color Tactical Briefings and preserving 100% of underlying
vector charts. All non-IAC charts (APD, DP, STAR, MIN) are strictly ignored.
"""

from __future__ import annotations

import io
import os
import sqlite3
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Generator, List, Optional, Set, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

from enhancer.compositor import export_enhanced_pdf_bytes, render_highlights
from enhancer.pdf_engine import load_pdf_plate
from enhancer.rules import RuleEngine


@dataclass
class IACPlateItem:
    file_path: Path
    file_name: str
    chart_name: str
    airport_ident: str
    icao_ident: str
    state: str
    chart_code: str = "IAC"

    @property
    def display_label(self) -> str:
        loc = self.icao_ident or self.airport_ident
        state_str = f" ({self.state})" if self.state else ""
        return f"{loc}{state_str} — {self.chart_name} [{self.file_name}]"


@dataclass
class BatchEnhanceResult:
    item: IACPlateItem
    success: bool
    highlights_count: int
    bytes_written: int
    duration: float
    error_message: Optional[str] = None


@dataclass
class BatchProgress:
    completed: int
    total: int
    current_item: IACPlateItem
    last_result: BatchEnhanceResult
    elapsed_seconds: float
    eta_seconds: float
    speed_per_sec: float


def find_plates_sqlite(root: Path) -> Optional[Path]:
    """Search for Plates.sqlite in target directory, standard subdirs, or immediate parent."""
    candidates = [
        root / "Plates.sqlite",
        root / "Plates" / "Plates.sqlite",
        root / "ChartData" / "Plates" / "Plates.sqlite",
        root.parent / "Plates.sqlite",
        root.parent / "Plates" / "Plates.sqlite",
        root.parent / "ChartData" / "Plates" / "Plates.sqlite",
    ]
    for c in candidates:
        if c.is_file():
            return c

    # Fallback to shallow glob if not in direct candidates
    try:
        found = list(root.glob("**/Plates.sqlite"))
        if found:
            return found[0]
    except Exception:
        pass

    return None


def find_dtpp_xml(root: Path) -> Optional[Path]:
    """Search for d-TPP_Metafile.xml in directory tree or immediate subdirectories."""
    candidates = [
        root / "d-TPP_Metafile.xml",
        root.parent / "d-TPP_Metafile.xml",
    ]
    for c in candidates:
        if c.is_file():
            return c

    try:
        found = list(root.glob("**/d-TPP_Metafile.xml"))
        if found:
            return found[0]
    except Exception:
        pass

    return None


def discover_iac_plates(target_dir: Path | str) -> Tuple[List[IACPlateItem], str]:
    """
    Scan `target_dir` and identify all official FAA IAC (Instrument Approach Chart / IAP)
    PDF files. Uses `Plates.sqlite` (or `d-TPP_Metafile.xml`) for authoritative metadata.
    Guarantees that APD, DP, STAR, MIN, and other non-IAC charts are excluded.

    Returns:
        `(iac_items, metadata_source_description)`
    """
    root = Path(target_dir).resolve()
    if not root.is_dir():
        return [], f"Target directory does not exist: {root}"

    # 1. First attempt: Plates.sqlite (standard in Airmate ChartData)
    sqlite_path = find_plates_sqlite(root)
    iac_metadata: Dict[str, Tuple[str, str, str, str, str]] = {}
    source_desc = ""

    if sqlite_path and sqlite_path.is_file():
        try:
            conn = sqlite3.connect(sqlite_path)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT c.File_Name, c.Chart_Name, c.Chart_Code,
                       COALESCE(a.Apt_Ident, ''),
                       COALESCE(a.ICAO_Ident, ''),
                       COALESCE(a.StateProvince, '')
                FROM Charts c
                LEFT JOIN Airports a ON c.Airport_Name_ID = a.Airport_Name_ID
                WHERE c.Chart_Code IN ('IAC', 'IAP', 'DAU')
                """
            )
            for row in cur.fetchall():
                fname, cname, ccode, apt, icao, state = row
                if fname:
                    fn_u = fname.upper().strip()
                    val = (
                        cname or "",
                        ccode or "IAC",
                        apt or "",
                        icao or "",
                        state or "",
                    )
                    iac_metadata[fn_u] = val
                    iac_metadata[Path(fn_u).stem] = val
            conn.close()
            source_desc = f"SQLite Database: {sqlite_path} ({len(iac_metadata):,} IAC records)"
        except Exception as err:
            source_desc = f"SQLite query error: {err}"

    # 2. Second attempt: d-TPP_Metafile.xml if SQLite was not found
    if not iac_metadata:
        xml_path = find_dtpp_xml(root)
        if xml_path and xml_path.is_file():
            try:
                tree = ET.parse(xml_path)
                xml_root = tree.getroot()
                for rec in xml_root.findall(".//record"):
                    ccode = (rec.findtext("chart_code") or "").strip().upper()
                    if ccode in ("IAP", "IAC", "DAU"):
                        pdf_name = (rec.findtext("pdf_name") or "").strip()
                        cname = (rec.findtext("chart_name") or "").strip()
                        apt = (rec.findtext("apt_ident") or "").strip()
                        icao = (rec.findtext("icao_ident") or "").strip()
                        state = (rec.findtext("state_code") or "").strip()
                        if pdf_name:
                            fn_u = pdf_name.upper().strip()
                            val = (cname, ccode, apt, icao, state)
                            iac_metadata[fn_u] = val
                            iac_metadata[Path(fn_u).stem] = val
                source_desc = f"FAA XML: {xml_path} ({len(iac_metadata):,} IAC records)"
            except Exception as err:
                source_desc = f"XML parse error: {err}"

    # Find all PDF and PNG files on disk inside root
    chart_files: List[Path] = []
    for ext in ("*.pdf", "*.PDF", "*.png", "*.PNG"):
        chart_files.extend(root.rglob(ext))

    # Deduplicate paths
    unique_files: Dict[Path, Path] = {p.resolve(): p for p in chart_files}

    # Match files against IAC catalog
    matched_items: List[IACPlateItem] = []
    for path in unique_files.values():
        name_u = path.name.upper()
        stem_u = path.stem.upper()
        if name_u in iac_metadata or stem_u in iac_metadata:
            cname, ccode, apt, icao, state = iac_metadata.get(name_u) or iac_metadata[stem_u]
            matched_items.append(
                IACPlateItem(
                    file_path=path,
                    file_name=path.name,
                    chart_name=cname,
                    airport_ident=apt,
                    icao_ident=icao,
                    state=state,
                    chart_code=ccode,
                )
            )

    # Sort deterministically by State, Airport, and Chart Name
    matched_items.sort(
        key=lambda x: (x.state or "ZZ", x.icao_ident or x.airport_ident or "ZZ", x.chart_name)
    )

    if not source_desc:
        source_desc = f"No SQLite or XML metadata found in {root}"

    return matched_items, source_desc


def enhance_single_iac_file(
    item: IACPlateItem,
    output_path: Optional[Path] = None,
    output_format: str = "PDF",
    aircraft_categories: Optional[List[str]] = None,
    target_approaches: Optional[List[str]] = None,
    enabled_colors: Optional[Set[str]] = None,
    opacity_scale: float = 1.0,
    dpi_scale: float = 3.0,
) -> BatchEnhanceResult:
    """
    Enhance a single IAC PDF file:
    1. Extract vector geometry and text tokens via `load_pdf_plate`.
    2. Evaluate deterministic 4-Color Tactical Briefing rules.
    3. Generate non-destructive transparent RGBA overlay layer and composited image.
    4. Save output as PDF (vector overlay), PNG (high-res RGBA image), or BOTH.
    """
    t0 = time.time()
    raw_path = item.file_path
    fmt = output_format.upper()

    try:
        # Load and extract vector paths & layout
        locked_base, tokens, lines, layout = load_pdf_plate(raw_path, dpi_scale=dpi_scale)

        rule_engine = RuleEngine(
            aircraft_categories=aircraft_categories or ["A", "B"],
            target_approaches=target_approaches or ["LPV", "LNAV/VNAV"],
            enabled_colors=enabled_colors or {"CYAN", "MAGENTA", "AMBER", "RED"},
        )
        highlights, _ = rule_engine.evaluate(tokens, lines, layout)

        composited, overlay = render_highlights(
            locked_base,
            highlights,
            opacity_scale=opacity_scale,
        )

        total_bytes_written = 0

        # Determine target PDF and PNG paths
        if output_path is not None:
            target_pdf = output_path.with_suffix(".PDF")
            target_png = output_path.with_suffix(".PNG")
        else:
            target_pdf = raw_path.with_suffix(".PDF")
            target_png = raw_path.with_suffix(".PNG")

        # 1. Output PDF if requested
        if fmt in ("PDF", "BOTH"):
            enhanced_pdf_bytes = export_enhanced_pdf_bytes(raw_path, overlay)
            target_pdf.parent.mkdir(parents=True, exist_ok=True)
            if target_pdf.resolve() == raw_path.resolve():
                tmp_path = target_pdf.with_name(f"{target_pdf.name}.tmp_{os.getpid()}_{time.time_ns()}")
                tmp_path.write_bytes(enhanced_pdf_bytes)
                tmp_path.replace(target_pdf)
            else:
                target_pdf.write_bytes(enhanced_pdf_bytes)
            total_bytes_written += len(enhanced_pdf_bytes)

        # 2. Output PNG if requested
        if fmt in ("PNG", "BOTH"):
            target_png.parent.mkdir(parents=True, exist_ok=True)
            tmp_png = target_png.with_name(f"{target_png.name}.tmp_{os.getpid()}_{time.time_ns()}")
            composited.save(tmp_png, format="PNG")
            tmp_png.replace(target_png)
            total_bytes_written += target_png.stat().st_size

        duration = time.time() - t0
        return BatchEnhanceResult(
            item=item,
            success=True,
            highlights_count=len(highlights),
            bytes_written=total_bytes_written,
            duration=duration,
        )
    except Exception as exc:
        duration = time.time() - t0
        return BatchEnhanceResult(
            item=item,
            success=False,
            highlights_count=0,
            bytes_written=0,
            duration=duration,
            error_message=str(exc),
        )


def run_batch_enhancement(
    items: List[IACPlateItem],
    output_dir: Optional[Path] = None,
    output_format: str = "PDF",
    aircraft_categories: Optional[List[str]] = None,
    target_approaches: Optional[List[str]] = None,
    enabled_colors: Optional[Set[str]] = None,
    opacity_scale: float = 1.0,
    max_workers: int = 6,
    progress_callback: Optional[Callable[[BatchProgress], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> List[BatchEnhanceResult]:
    """
    Run multithreaded enhancement across `items`.
    Outputs PDF, PNG, or BOTH based on `output_format`.
    Reports progress in real-time via `progress_callback`.
    Stops gracefully if `cancel_check()` returns True.
    """
    total = len(items)
    if total == 0:
        return []

    results: List[BatchEnhanceResult] = []
    completed = 0
    t_start = time.time()

    def _worker(item: IACPlateItem) -> BatchEnhanceResult:
        out_p = (output_dir / item.file_name) if output_dir is not None else None
        return enhance_single_iac_file(
            item=item,
            output_path=out_p,
            output_format=output_format,
            aircraft_categories=aircraft_categories,
            target_approaches=target_approaches,
            enabled_colors=enabled_colors,
            opacity_scale=opacity_scale,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_worker, item): item for item in items}
        for future in as_completed(futures):
            if cancel_check and cancel_check():
                executor.shutdown(wait=False, cancel_futures=True)
                break

            res = future.result()
            results.append(res)
            completed += 1

            elapsed = time.time() - t_start
            speed = completed / elapsed if elapsed > 0 else 0.0
            remaining = total - completed
            eta = (remaining / speed) if speed > 0 else 0.0

            if progress_callback:
                prog = BatchProgress(
                    completed=completed,
                    total=total,
                    current_item=res.item,
                    last_result=res,
                    elapsed_seconds=elapsed,
                    eta_seconds=eta,
                    speed_per_sec=speed,
                )
                progress_callback(prog)

    return results
