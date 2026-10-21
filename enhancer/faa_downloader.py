"""
FAA d-TPP Raw Data Downloader & Extractor (`enhancer/faa_downloader.py`).

Downloads official FAA d-TPP volume zip archives (DDTPPA..E) from:
https://aeronav.faa.gov/upload_313-d/terminal/
and extracts them directly into the target FAA PDF data directory:
e.g. /Users/GFahmy/Documents/RV-7/Airmate/FAA_pdf_data/DDTPPE_261001/d-TPP_Metafile.xml
"""

from __future__ import annotations

import os
import re
import shutil
import ssl
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

FAA_DTPP_WEBPAGE = "https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/dtpp/"
FAA_BASE_URL = "https://aeronav.faa.gov/upload_313-d/terminal"
DEFAULT_VOLUMES = ["A", "B", "C", "D", "E"]
DEFAULT_EDITION = "261001"


@dataclass
class DownloadProgress:
    stage: str  # "connecting", "downloading", "extracting", "complete", "error"
    volume: str  # e.g. "DDTPPA"
    volume_index: int  # 1-based (e.g. 1)
    total_volumes: int  # e.g. 5
    bytes_downloaded: int
    total_bytes: int
    speed_bps: float
    elapsed_seconds: float
    eta_seconds: float
    message: str
    is_success: bool = True


def get_ssl_context() -> ssl.SSLContext:
    """Create a reliable SSL context supporting macOS python installations."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        try:
            return ssl.create_default_context()
        except Exception:
            return ssl._create_unverified_context()


def fetch_available_editions_with_metadata(timeout: float = 8.0) -> List[Dict[str, str]]:
    """
    Scrape official FAA d-TPP website to detect available cycle edition codes
    and their effective date ranges (from HTML table captions).
    Returns list of dicts: [{'edition': '261029', 'date_range': 'Oct 29 2026 – Nov 26 2026', 'label': '...'}]
    """
    results: List[Dict[str, str]] = []
    seen: Set[str] = set()

    try:
        ctx = get_ssl_context()
        req = urllib.request.Request(
            FAA_DTPP_WEBPAGE,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        # Match table captions followed by DDTPPA_xxxxxx.zip
        pattern = re.compile(
            r"<caption[^>]*>(.*?)</caption>.*?DDTPPA_(\d{6})\.zip",
            re.DOTALL | re.IGNORECASE,
        )
        matches = pattern.findall(html)
        for caption, ed in matches:
            if ed not in seen:
                seen.add(ed)
                clean_cap = re.sub(r"&ndash;|&mdash;", " – ", caption)
                clean_cap = re.sub(r"\s+", " ", clean_cap).strip()
                results.append({
                    "edition": ed,
                    "date_range": clean_cap,
                    "label": f"{ed} ({clean_cap})" if clean_cap else ed,
                })

        # Fallback: scan for any DDTPPA_xxxxxx.zip
        all_editions = sorted(set(re.findall(r"DDTPPA_(\d{6})\.zip", html)))
        for ed in all_editions:
            if ed not in seen:
                seen.add(ed)
                results.append({
                    "edition": ed,
                    "date_range": "",
                    "label": ed,
                })
    except Exception:
        pass

    if not results:
        results = [
            {
                "edition": "261029",
                "date_range": "Oct 29 2026 – Nov 26 2026",
                "label": "261029 (Oct 29 2026 – Nov 26 2026)",
            },
            {
                "edition": "261001",
                "date_range": "Sep 03 2026 – Oct 29 2026",
                "label": "261001 (Sep 03 2026 – Oct 29 2026)",
            },
        ]

    # Sort descending by edition code so the latest edition is first
    results.sort(key=lambda d: d["edition"], reverse=True)
    return results


def fetch_available_editions(timeout: float = 8.0) -> List[str]:
    """
    Scrape official FAA d-TPP website to detect available cycle edition codes
    (e.g. ['261029', '261001']). Falls back to defaults on error.
    """
    meta_list = fetch_available_editions_with_metadata(timeout=timeout)
    return [m["edition"] for m in meta_list]


def get_volume_urls(
    edition: str = DEFAULT_EDITION,
    volumes: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Generate mapping of volume codes to FAA download URLs."""
    vols = volumes or DEFAULT_VOLUMES
    return {
        f"DDTPP{v}": f"{FAA_BASE_URL}/DDTPP{v}_{edition}.zip"
        for v in vols
    }


def download_file(
    url: str,
    dest_path: Path,
    volume_name: str,
    volume_idx: int,
    total_vols: int,
    progress_callback: Optional[Callable[[DownloadProgress], None]] = None,
    chunk_size: int = 1024 * 1024,  # 1 MB chunk
) -> int:
    """
    Download a single file from URL to dest_path with live chunk progress updates.
    Returns total bytes downloaded.
    """
    ctx = get_ssl_context()
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
    )

    t_start = time.time()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_name(f"{dest_path.name}.part")

    if progress_callback:
        progress_callback(
            DownloadProgress(
                stage="connecting",
                volume=volume_name,
                volume_index=volume_idx,
                total_volumes=total_vols,
                bytes_downloaded=0,
                total_bytes=0,
                speed_bps=0.0,
                elapsed_seconds=0.0,
                eta_seconds=0.0,
                message=f"Connecting to {url} ...",
            )
        )

    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        total_size = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        last_cb_time = 0.0

        with open(temp_path, "wb") as f_out:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f_out.write(chunk)
                downloaded += len(chunk)

                now = time.time()
                elapsed = max(0.001, now - t_start)
                speed = downloaded / elapsed
                eta = (total_size - downloaded) / speed if (total_size > downloaded and speed > 0) else 0.0

                if progress_callback and (now - last_cb_time >= 0.2 or downloaded == total_size):
                    last_cb_time = now
                    progress_callback(
                        DownloadProgress(
                            stage="downloading",
                            volume=volume_name,
                            volume_index=volume_idx,
                            total_volumes=total_vols,
                            bytes_downloaded=downloaded,
                            total_bytes=total_size,
                            speed_bps=speed,
                            elapsed_seconds=elapsed,
                            eta_seconds=eta,
                            message=f"Downloading {volume_name} ({downloaded / 1024 / 1024:.1f} MB / {total_size / 1024 / 1024:.1f} MB)",
                        )
                    )

    if temp_path.exists():
        temp_path.replace(dest_path)
    return downloaded


def extract_volume_zip(
    zip_path: Path,
    target_extract_dir: Path,
    volume_name: str,
    volume_idx: int,
    total_vols: int,
    progress_callback: Optional[Callable[[DownloadProgress], None]] = None,
) -> int:
    """Extract a volume zip into target_extract_dir with progress reporting."""
    target_extract_dir.mkdir(parents=True, exist_ok=True)
    t_start = time.time()

    with zipfile.ZipFile(zip_path, "r") as zf:
        members = zf.infolist()
        total_files = len(members)
        last_cb_time = 0.0

        for idx, member in enumerate(members):
            zf.extract(member, target_extract_dir)

            now = time.time()
            if progress_callback and (now - last_cb_time >= 0.25 or idx + 1 == total_files):
                last_cb_time = now
                elapsed = max(0.001, now - t_start)
                speed = (idx + 1) / elapsed
                eta = (total_files - (idx + 1)) / speed if speed > 0 else 0.0
                progress_callback(
                    DownloadProgress(
                        stage="extracting",
                        volume=volume_name,
                        volume_index=volume_idx,
                        total_volumes=total_vols,
                        bytes_downloaded=idx + 1,
                        total_bytes=total_files,
                        speed_bps=speed,
                        elapsed_seconds=elapsed,
                        eta_seconds=eta,
                        message=f"Extracting {volume_name}: {idx + 1}/{total_files} files ({member.filename})",
                    )
                )

    return len(members)


def download_and_extract_faa_volumes(
    edition: str = DEFAULT_EDITION,
    target_dir: Path = Path("/Users/GFahmy/Documents/RV-7/Airmate/FAA_pdf_data"),
    volumes: Optional[List[str]] = None,
    delete_zips_after_extract: bool = True,
    progress_callback: Optional[Callable[[DownloadProgress], None]] = None,
) -> Dict[str, object]:
    """
    Main orchestration routine to download and extract FAA d-TPP volumes:
    1. Downloads DDTPPA..E zip files into target_dir
    2. Extracts each into target_dir / f"DDTPP{v}_{edition}"
    3. Removes temporary zips if delete_zips_after_extract is True
    4. Identifies the primary d-TPP_Metafile.xml in DDTPPE
    """
    t_total_start = time.time()
    vols = [v.upper() for v in (volumes or DEFAULT_VOLUMES)]
    urls = get_volume_urls(edition, vols)
    total_vols = len(vols)
    extracted_dirs: Dict[str, Path] = {}
    total_bytes = 0

    target_dir = Path(target_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    for idx, vol in enumerate(vols, start=1):
        vol_name = f"DDTPP{vol}"
        url = urls[vol_name]
        zip_file = target_dir / f"{vol_name}_{edition}.zip"
        extract_folder = target_dir / f"{vol_name}_{edition}"

        # 1. Download
        bytes_dl = download_file(
            url=url,
            dest_path=zip_file,
            volume_name=vol_name,
            volume_idx=idx,
            total_vols=total_vols,
            progress_callback=progress_callback,
        )
        total_bytes += bytes_dl

        # 2. Extract
        extract_volume_zip(
            zip_path=zip_file,
            target_extract_dir=extract_folder,
            volume_name=vol_name,
            volume_idx=idx,
            total_vols=total_vols,
            progress_callback=progress_callback,
        )
        extracted_dirs[vol_name] = extract_folder

        # 3. Clean up zip
        if delete_zips_after_extract and zip_file.exists():
            try:
                zip_file.unlink()
            except Exception:
                pass

    total_elapsed = time.time() - t_total_start
    xml_path = extracted_dirs.get("DDTPPE", target_dir / f"DDTPPE_{edition}") / "d-TPP_Metafile.xml"

    if progress_callback:
        progress_callback(
            DownloadProgress(
                stage="complete",
                volume="ALL",
                volume_index=total_vols,
                total_volumes=total_vols,
                bytes_downloaded=total_bytes,
                total_bytes=total_bytes,
                speed_bps=total_bytes / max(0.001, total_elapsed),
                elapsed_seconds=total_elapsed,
                eta_seconds=0.0,
                message=f"Successfully downloaded and extracted {total_vols} FAA volumes!",
            )
        )

    return {
        "edition": edition,
        "target_dir": target_dir,
        "extracted_dirs": extracted_dirs,
        "xml_path": xml_path if xml_path.is_file() else None,
        "total_bytes": total_bytes,
        "elapsed_seconds": total_elapsed,
        "volumes": vols,
        "success": True,
    }
