"""
Desktop API Bridge for Native WebKit Application (`desktop_api.py`).

Provides direct bidirectional bindings between JavaScript and the Python
enhancement engine, plate browser, and Dynon/FAA pipeline without Streamlit.
"""

from __future__ import annotations

import base64
import io
import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import webview
from PIL import Image

from cli import enhance_pdf_plate
from enhancer.compositor import export_enhanced_pdf_bytes
from enhancer.dynon_nav_downloader import (
    DEFAULT_DYNON_NAV_DIR,
    DynonNavProgress,
    download_dynon_database,
    fetch_available_dynon_databases,
)
from enhancer.faa_downloader import (
    DownloadProgress,
    download_and_extract_faa_volumes,
    fetch_available_editions_with_metadata,
)
from enhancer.pipeline_controller import PipelineController
from enhancer.plate_browser import DEFAULT_FAA_PDF_DIR, DTPPPlateBrowser
from recreate_airmate_chartdata import (
    DEFAULT_OUTPUT_DIR,
    DEFAULT_WORKERS,
    DEFAULT_XML_PATH,
    RecreateProgress,
    build_chartdata,
    find_latest_dtpp_xml,
)


def _chart_sort_key(chart) -> Tuple[int, str, str]:
    code = (chart.chart_code or "").upper()
    if code in ("IAP", "IAC", "DAU"):
        group = 0
    elif code == "APD":
        group = 1
    elif code in ("DP", "ODP"):
        group = 2
    elif code in ("STR", "STAR"):
        group = 3
    elif code == "MIN":
        group = 4
    elif code == "HOT":
        group = 5
    elif code == "LAH":
        group = 6
    else:
        group = 7
    return (group, chart.chart_name, chart.file_name)


class DesktopAPI:
    def __init__(self) -> None:
        self.window: Optional[webview.Window] = None
        self._browser: Optional[DTPPPlateBrowser] = None
        self._pipeline_running = False
        self._controller: Optional[PipelineController] = None

    def pause_pipeline(self) -> Dict[str, Any]:
        """Pause running pipeline at the next check-point."""
        if self._controller and self._pipeline_running:
            self._controller.pause()
            return {"success": True, "is_paused": True}
        return {"success": False, "error": "No active pipeline to pause"}

    def resume_pipeline(self) -> Dict[str, Any]:
        """Resume paused pipeline."""
        if self._controller and self._pipeline_running:
            self._controller.resume()
            return {"success": True, "is_paused": False}
        return {"success": False, "error": "No active pipeline to resume"}

    def stop_pipeline(self) -> Dict[str, Any]:
        """Stop and cancel running pipeline."""
        if self._controller and self._pipeline_running:
            self._controller.stop()
            return {"success": True, "is_stopped": True}
        return {"success": False, "error": "No active pipeline to stop"}

    def set_window(self, window: webview.Window) -> None:
        self.window = window

    @property
    def browser(self) -> DTPPPlateBrowser:
        if self._browser is None:
            self._browser = DTPPPlateBrowser()
        return self._browser

    def init_app(self) -> Dict[str, Any]:
        """Return initial configuration, default paths, and initial catalog status."""
        b = self.browser
        editions = fetch_available_editions_with_metadata()
        latest_xml = find_latest_dtpp_xml(DEFAULT_FAA_PDF_DIR) or DEFAULT_XML_PATH

        default_airport = None
        default_charts = []
        if b.is_available:
            e16_matches = b.filter_airports(search_query="E16")
            if e16_matches:
                apt = e16_matches[0]
                default_airport = {
                    "id": apt.airport_name_id,
                    "ident": apt.icao_ident or apt.apt_ident,
                    "name": apt.airport_name,
                    "city": apt.city,
                    "state": apt.state_code,
                    "iac_count": apt.iac_count,
                    "label": apt.display_label,
                }
                charts = b.get_charts_for_airport(apt.airport_name_id)
                charts.sort(key=_chart_sort_key)
                default_charts = [
                    {
                        "chart_code": c.chart_code,
                        "chart_name": c.chart_name,
                        "file_name": c.file_name,
                        "pdf_path": str(c.file_path),
                        "exists": c.exists,
                        "label": c.display_label,
                        "is_approach": c.chart_code.upper() in ("IAP", "IAC", "DAU"),
                    }
                    for c in charts
                ]

        dynon_dbs = fetch_available_dynon_databases()

        return {
            "is_catalog_available": b.is_available,
            "cycle": b.cycle,
            "from_edate": b.from_edate,
            "to_edate": b.to_edate,
            "total_airports": len(b._airports) if b.is_available else 0,
            "xml_path": str(b.xml_path) if b.xml_path else str(latest_xml),
            "faa_dir": str(DEFAULT_FAA_PDF_DIR),
            "output_dir": str(DEFAULT_OUTPUT_DIR),
            "dynon_nav_dir": str(DEFAULT_DYNON_NAV_DIR),
            "dynon_databases": dynon_dbs,
            "available_editions": editions,
            "default_airport": default_airport,
            "default_charts": default_charts,
        }

    def search_airports(self, query: str, limit: int = 15) -> List[Dict[str, Any]]:
        """Search airports by ICAO, FAA LID, city, or airport name."""
        b = self.browser
        if not b.is_available:
            return []
        matches = b.filter_airports(search_query=query, only_with_iac=False)[:limit]
        return [
            {
                "id": a.airport_name_id,
                "ident": a.icao_ident or a.apt_ident,
                "name": a.airport_name,
                "city": a.city,
                "state": a.state_code,
                "iac_count": a.iac_count,
                "label": a.display_label,
            }
            for a in matches
        ]

    def get_airport_charts(self, airport_id: str) -> List[Dict[str, Any]]:
        """Get all terminal procedure charts for a given airport (approach plates sorted first)."""
        b = self.browser
        if not b.is_available:
            return []
        charts = b.get_charts_for_airport(airport_id)
        charts.sort(key=_chart_sort_key)
        return [
            {
                "chart_code": c.chart_code,
                "chart_name": c.chart_name,
                "file_name": c.file_name,
                "pdf_path": str(c.file_path),
                "exists": c.exists,
                "label": c.display_label,
                "is_approach": c.chart_code.upper() in ("IAP", "IAC", "DAU"),
            }
            for c in charts
        ]

    def render_plate(
        self,
        pdf_path: str,
        colors: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        approaches: Optional[List[str]] = None,
        opacity: float = 1.0,
        chart_code: Optional[str] = None,
        view_mode: str = "enhanced",
    ) -> Dict[str, Any]:
        """Render original or enhanced plate images as Base64 data URLs."""
        p = Path(pdf_path)
        if not p.is_file():
            return {"success": False, "error": f"File not found: {pdf_path}"}

        is_approach = (
            chart_code.upper() in ("IAP", "IAC", "DAU") if chart_code else True
        )

        # FAST PATH: Instant sub-50ms rasterization for original FAA plate viewer
        if view_mode == "original":
            try:
                import pymupdf

                doc = pymupdf.open(str(p))
                page = doc[0]
                mat = pymupdf.Matrix(2.5, 2.5)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                png_bytes = pix.tobytes(output="png")
                w, h = pix.width, pix.height
                del pix
                doc.close()
                del doc

                orig_b64 = "data:image/png;base64," + base64.b64encode(
                    png_bytes
                ).decode("ascii")
                return {
                    "success": True,
                    "original_image": orig_b64,
                    "enhanced_image": None,
                    "briefing": "### Official FAA Chart\n\nViewing standard official plate. Switch to **Enhanced** or **Split** view to generate 4-color tactical highlights.",
                    "chart_name": p.stem,
                    "width": w,
                    "height": h,
                    "highlights_count": 0,
                    "is_approach": is_approach,
                    "document_type": chart_code
                    or ("IAP" if is_approach else "STANDARD"),
                }
            except Exception as e:
                return {"success": False, "error": str(e)}

        try:
            enabled_colors = (
                set(colors) if colors else {"CYAN", "MAGENTA", "AMBER", "RED"}
            )
            cats = categories or ["A", "B"]
            appr = approaches or ["LPV", "LNAV/VNAV"]

            composited, overlay, highlights, metadata, briefing_md, locked_base = (
                enhance_pdf_plate(
                    p,
                    dpi_scale=2.5,
                    aircraft_categories=cats,
                    target_approaches=appr,
                    enabled_colors=enabled_colors,
                    opacity_scale=opacity,
                    chart_code=chart_code,
                    return_base=True,
                )
            )

            # Convert to Base64 data URLs
            def _to_b64(img: Image.Image) -> str:
                buf = io.BytesIO()
                img.save(buf, format="PNG", optimize=True)
                return "data:image/png;base64," + base64.b64encode(
                    buf.getvalue()
                ).decode("ascii")

            enh_b64 = _to_b64(composited)
            orig_b64 = _to_b64(locked_base)

            is_approach = getattr(metadata, "is_approach_plate", True)

            return {
                "success": True,
                "enhanced_image": enh_b64,
                "original_image": orig_b64,
                "briefing": briefing_md,
                "chart_name": p.stem,
                "width": composited.width,
                "height": composited.height,
                "highlights_count": len(highlights),
                "is_approach": is_approach,
                "document_type": getattr(
                    metadata, "document_type", chart_code or "IAP"
                ),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def save_plate_file(
        self,
        pdf_path: str,
        format_type: str,
        colors: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        approaches: Optional[List[str]] = None,
        opacity: float = 1.0,
        chart_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Save enhanced PNG or PDF using native macOS Save Dialog."""
        if not self.window:
            return {"success": False, "error": "Window not initialized"}

        p = Path(pdf_path)
        if not p.is_file():
            return {"success": False, "error": f"File not found: {pdf_path}"}

        default_name = f"enhanced_{p.stem}.{format_type.lower()}"
        file_types = (
            ("PNG Images (*.png)", "*.png")
            if format_type.lower() == "png"
            else ("PDF Documents (*.pdf)", "*.pdf")
        )

        res = self.window.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=default_name,
            file_types=[file_types[0]],
        )
        if not res:
            return {"success": False, "cancelled": True}

        raw_path = str(res[0]) if isinstance(res, (list, tuple)) else str(res)
        save_path = Path(raw_path)

        try:
            enabled_colors = (
                set(colors) if colors else {"CYAN", "MAGENTA", "AMBER", "RED"}
            )
            cats = categories or ["A", "B"]
            appr = approaches or ["LPV", "LNAV/VNAV"]

            composited, overlay, _, _, _ = enhance_pdf_plate(
                p,
                dpi_scale=3.0,
                aircraft_categories=cats,
                target_approaches=appr,
                enabled_colors=enabled_colors,
                opacity_scale=opacity,
                chart_code=chart_code,
            )

            if format_type.lower() == "png":
                composited.save(save_path, format="PNG")
            else:
                raw_bytes = p.read_bytes()
                pdf_bytes = export_enhanced_pdf_bytes(raw_bytes, overlay)
                save_path.write_bytes(pdf_bytes)

            return {"success": True, "saved_path": str(save_path)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def select_directory(self, current_path: str = "") -> Dict[str, Any]:
        """Open native macOS folder picker dialog."""
        if not self.window:
            return {"cancelled": True}
        res = self.window.create_file_dialog(
            webview.FileDialog.FOLDER,
            directory=current_path or str(Path.home()),
        )
        if not res:
            return {"cancelled": True}
        selected = res[0] if isinstance(res, (list, tuple)) else res
        return {"cancelled": False, "path": str(selected)}

    def run_pipeline(
        self,
        action: str,  # "download_only", "generate_only", "both"
        edition: str,
        faa_dir: str,
        output_dir: str,
        format_type: str = "png",
        png_mode: str = "enhanced",
        dpi: int = 200,
        workers: int = DEFAULT_WORKERS,
        delete_zips: bool = True,
        include_nav: bool = False,
        nav_url: str = "",
        nav_dir: str = "",
        nav_filename: str = "",
    ) -> None:
        """Execute FAA download and/or ChartData build in background thread with live UI updates."""
        if self._pipeline_running:
            return
        self._pipeline_running = True
        self._controller = PipelineController()
        controller = self._controller

        def _worker() -> None:
            try:
                target_faa = Path(faa_dir)
                target_out = Path(output_dir)
                actual_workers = workers if workers and workers > 0 else DEFAULT_WORKERS

                def _emit(data: Dict[str, Any]) -> None:
                    if self.window:
                        js_code = f"window.__onPipelineProgress({json.dumps(data)});"
                        self.window.evaluate_js(js_code)

                # 0. Download Dynon Nav (.duc) if requested
                if include_nav and nav_url:
                    if not controller.check_point():
                        _emit(
                            {
                                "stage": "stopped",
                                "status": "🛑 Pipeline stopped by user",
                                "is_stopped": True,
                            }
                        )
                        return

                    _emit(
                        {
                            "stage": "downloading",
                            "status": f"Downloading Dynon Nav DB: {nav_filename}...",
                            "progress": 0.0,
                        }
                    )

                    def _nav_cb(p: DynonNavProgress) -> None:
                        pct = (
                            (p.bytes_downloaded / max(1, p.total_bytes))
                            if p.total_bytes > 0
                            else 0.0
                        )
                        _emit(
                            {
                                "stage": "download",
                                "status": p.message,
                                "progress": pct,
                                "speed": f"{p.speed_bps / 1024 / 1024:.2f} MB/s",
                                "eta": f"{p.eta_seconds:.0f}s",
                                "volume": p.filename,
                            }
                        )

                    download_dynon_database(
                        url=nav_url,
                        dest_dir=nav_dir or DEFAULT_DYNON_NAV_DIR,
                        filename=nav_filename,
                        progress_callback=_nav_cb,
                    )

                xml_path = None

                # 1. Download FAA Data if requested
                if action in ("download_only", "both"):
                    if not controller.check_point():
                        _emit(
                            {
                                "stage": "stopped",
                                "status": "🛑 Pipeline stopped by user",
                                "is_stopped": True,
                            }
                        )
                        return

                    _emit(
                        {
                            "stage": "downloading",
                            "status": "Starting FAA d-TPP download...",
                            "progress": 0.0,
                            "detail": f"Cycle {edition}",
                        }
                    )

                    def _dl_cb(p: DownloadProgress) -> None:
                        pct = (
                            min(
                                1.0,
                                (
                                    p.volume_index
                                    - 1
                                    + (p.bytes_downloaded / max(1, p.total_bytes))
                                )
                                / max(1, p.total_volumes),
                            )
                            if p.total_bytes > 0
                            else 0.0
                        )
                        _emit(
                            {
                                "stage": "download",
                                "status": p.message,
                                "progress": pct,
                                "speed": (
                                    f"{p.speed_bps / 1024 / 1024:.2f} MB/s"
                                    if p.stage == "downloading"
                                    else f"{p.speed_bps:.1f} files/s"
                                ),
                                "eta": f"{p.eta_seconds:.0f}s",
                                "volume": f"{p.volume} ({p.volume_index}/{p.total_volumes})",
                            }
                        )

                    dl_res = download_and_extract_faa_volumes(
                        edition=edition,
                        target_dir=target_faa,
                        delete_zips_after_extract=delete_zips,
                        progress_callback=_dl_cb,
                        controller=controller,
                    )

                    if controller.is_stopped or dl_res.get("stopped"):
                        _emit(
                            {
                                "stage": "stopped",
                                "status": "🛑 Download stopped by user",
                                "is_stopped": True,
                            }
                        )
                        return

                    xml_path = dl_res.get("xml_path")

                    # Refresh local browser index
                    self.browser.refresh()

                # 2. Generate ChartData if requested
                if action in ("generate_only", "both"):
                    if not controller.check_point():
                        _emit(
                            {
                                "stage": "stopped",
                                "status": "🛑 Pipeline stopped by user",
                                "is_stopped": True,
                            }
                        )
                        return

                    if not xml_path:
                        xml_path = find_latest_dtpp_xml(target_faa) or DEFAULT_XML_PATH

                    _emit(
                        {
                            "stage": "generating",
                            "status": "Initializing Dynon ChartData generator...",
                            "progress": 0.0,
                            "detail": f"Format: {format_type.upper()}",
                        }
                    )

                    def _gen_cb(p: RecreateProgress) -> None:
                        pct = (
                            min(1.0, p.completed / max(1, p.total))
                            if p.total > 0
                            else 0.0
                        )
                        _emit(
                            {
                                "stage": "generate",
                                "status": f"[{p.stage.upper()}] {p.message}",
                                "progress": pct,
                                "speed": f"{p.speed_per_sec:.1f} pl/s",
                                "eta": f"{p.eta_seconds:.0f}s",
                                "completed": p.completed,
                                "total": p.total,
                            }
                        )

                    summary = build_chartdata(
                        xml_path=Path(str(xml_path)),
                        faa_data_dir=target_faa,
                        output_dir=target_out,
                        output_format=format_type,
                        png_mode=png_mode,
                        dpi=dpi,
                        workers=actual_workers,
                        progress_callback=_gen_cb,
                        controller=controller,
                    )

                    if controller.is_stopped or summary.get("stopped"):
                        _emit(
                            {
                                "stage": "stopped",
                                "status": "🛑 Generation stopped by user",
                                "is_stopped": True,
                            }
                        )
                        return

                    _emit(
                        {
                            "stage": "complete",
                            "status": "Finished!",
                            "progress": 1.0,
                            "summary": {
                                "total_charts": summary.get("total_charts", 0),
                                "airports": summary.get("total_airports", 0),
                                "time": f"{summary.get('total_time', 0):.1f}s",
                                "output_dir": str(target_out),
                            },
                        }
                    )
                else:
                    _emit(
                        {
                            "stage": "complete",
                            "status": "Download complete!",
                            "progress": 1.0,
                        }
                    )

            except Exception as exc:
                if self.window:
                    err_msg = json.dumps({"stage": "error", "error": str(exc)})
                    self.window.evaluate_js(f"window.__onPipelineProgress({err_msg});")
            finally:
                self._pipeline_running = False
                self._controller = None

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    def download_dynon_nav_database(
        self,
        url: str,
        dest_dir: str,
        filename: str,
    ) -> None:
        """Download single Dynon .duc database in background thread."""
        if self._pipeline_running:
            return
        self._pipeline_running = True

        def _worker() -> None:
            try:

                def _emit(data: Dict[str, Any]) -> None:
                    if self.window:
                        js_code = f"window.__onPipelineProgress({json.dumps(data)});"
                        self.window.evaluate_js(js_code)

                _emit(
                    {
                        "stage": "download",
                        "status": f"Downloading Dynon DB: {filename}...",
                        "progress": 0.0,
                    }
                )

                def _cb(p: DynonNavProgress) -> None:
                    pct = (
                        (p.bytes_downloaded / max(1, p.total_bytes))
                        if p.total_bytes > 0
                        else 0.0
                    )
                    _emit(
                        {
                            "stage": "download",
                            "status": p.message,
                            "progress": pct,
                            "speed": f"{p.speed_bps / 1024 / 1024:.2f} MB/s",
                            "eta": f"{p.eta_seconds:.0f}s",
                            "volume": p.filename,
                        }
                    )

                saved_file = download_dynon_database(
                    url=url,
                    dest_dir=dest_dir or DEFAULT_DYNON_NAV_DIR,
                    filename=filename,
                    progress_callback=_cb,
                )

                _emit(
                    {
                        "stage": "complete",
                        "status": f"Successfully downloaded {filename}!",
                        "progress": 1.0,
                        "summary": {
                            "saved_file": str(saved_file),
                            "output_dir": str(Path(dest_dir or DEFAULT_DYNON_NAV_DIR)),
                        },
                    }
                )
            except Exception as exc:
                if self.window:
                    err_msg = json.dumps({"stage": "error", "error": str(exc)})
                    self.window.evaluate_js(f"window.__onPipelineProgress({err_msg});")
            finally:
                self._pipeline_running = False

        threading.Thread(target=_worker, daemon=True).start()
