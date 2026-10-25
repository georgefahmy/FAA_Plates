"""
Dynon Avionics Aviation & Obstacle Database Downloader (`enhancer/dynon_nav_downloader.py`).

Scrapes and downloads official Dynon SkyView aviation and obstacle database files (.duc)
from https://dynonavionics.com/us-aviation-obstacle-data.php directly into the user's
local software folder (e.g. /Users/GFahmy/Documents/RV-7/Software/sv_software) or USB drive.
"""

from __future__ import annotations

import os
import re
import ssl
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin

DYNON_NAV_PAGE_URL = "https://dynonavionics.com/us-aviation-obstacle-data.php"
DEFAULT_DYNON_NAV_DIR = Path("/Users/GFahmy/Documents/RV-7/Software/sv_software")


@dataclass
class DynonNavProgress:
    filename: str
    bytes_downloaded: int
    total_bytes: int
    speed_bps: float
    elapsed_seconds: float
    eta_seconds: float
    message: str
    is_complete: bool = False


def _get_ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        try:
            return ssl.create_default_context()
        except Exception:
            return ssl._create_unverified_context()


def fetch_available_dynon_databases(timeout: float = 10.0) -> List[Dict[str, Any]]:
    """
    Scrape Dynon Avionics website to detect available Aviation & Obstacle (.duc) databases,
    extracting filenames, URLs, cycle codes, and validity date ranges.
    """
    databases: List[Dict[str, Any]] = []

    try:
        ctx = _get_ssl_context()
        req = urllib.request.Request(
            DYNON_NAV_PAGE_URL,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
        )
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        blocks = re.findall(
            r"<div class=\"download-block\"[^>]*>(.*?)</div>",
            html,
            re.DOTALL | re.IGNORECASE,
        )

        for block in blocks:
            link_m = re.search(r"href=[\"\']([^\"\']+\.duc)[\"\']", block, re.IGNORECASE)
            if not link_m:
                continue

            duc_rel = link_m.group(1)
            duc_url = urljoin(DYNON_NAV_PAGE_URL, duc_rel)
            filename = duc_rel.split("/")[-1]

            # Parse validity text
            av_m = re.search(
                r"Aviation Database:[^<]*<span[^>]*>([^<]+)</span>",
                block,
                re.IGNORECASE,
            )
            ob_m = re.search(
                r"Obstacle Database:[^<]*<span[^>]*>([^<]+)</span>",
                block,
                re.IGNORECASE,
            )

            av_text = av_m.group(1).strip() if av_m else ""
            ob_text = ob_m.group(1).strip() if ob_m else ""

            # Extract cycle number if present, e.g. "Cycle: 2611"
            cyc_m = re.search(r"Cycle:\s*(\d{4})", av_text)
            cycle_code = cyc_m.group(1) if cyc_m else ""

            is_upcoming = "upcoming" in block.lower() or "next" in block.lower()
            tag = " [Upcoming]" if is_upcoming else " [Current]"

            databases.append({
                "filename": filename,
                "url": duc_url,
                "cycle": cycle_code,
                "aviation_validity": av_text,
                "obstacle_validity": ob_text,
                "is_upcoming": is_upcoming,
                "label": f"{filename}{tag} — {av_text}",
            })

    except Exception:
        pass

    if not databases:
        # Fallback offline defaults if network unreachable
        databases = [
            {
                "filename": "FAA_av2611_ob2606.duc",
                "url": "https://dynonavionics.com/downloads/Software/Us-av-ob/FAA_av2611_ob2606.duc",
                "cycle": "2611",
                "aviation_validity": "Valid: October 29 - November 25 (Cycle: 2611)",
                "obstacle_validity": "Valid: October 29 - December 23 (Cycle: 2606)",
                "is_upcoming": True,
                "label": "FAA_av2611_ob2606.duc [Upcoming] — Valid: October 29 - November 25 (Cycle: 2611)",
            },
            {
                "filename": "FAA_av2610_ob2605.duc",
                "url": "https://dynonavionics.com/downloads/Software/Us-av-ob/FAA_av2610_ob2605.duc",
                "cycle": "2610",
                "aviation_validity": "Valid: October 1 - October 28 (Cycle: 2610)",
                "obstacle_validity": "Valid: September 3 - October 28 (Cycle: 2605)",
                "is_upcoming": False,
                "label": "FAA_av2610_ob2605.duc [Current] — Valid: October 1 - October 28 (Cycle: 2610)",
            },
        ]

    # Sort upcoming first, or newest cycle first
    databases.sort(key=lambda d: d.get("cycle", ""), reverse=True)
    return databases


def download_dynon_database(
    url: str,
    dest_dir: Path | str,
    filename: Optional[str] = None,
    progress_callback: Optional[Callable[[DynonNavProgress], None]] = None,
    chunk_size: int = 512 * 1024,
) -> Path:
    """
    Download a single Dynon .duc database file with live streaming progress updates.
    Returns path to downloaded file.
    """
    dest_path = Path(dest_dir).resolve()
    dest_path.mkdir(parents=True, exist_ok=True)

    fname = filename or url.split("/")[-1]
    final_file = dest_path / fname
    temp_file = dest_path / f"{fname}.part"

    ctx = _get_ssl_context()
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
    )

    t_start = time.time()
    last_cb = 0.0

    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        total_size = int(resp.headers.get("Content-Length", 0))
        downloaded = 0

        with open(temp_file, "wb") as f_out:
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

                if progress_callback and (now - last_cb >= 0.2 or downloaded == total_size):
                    last_cb = now
                    progress_callback(
                        DynonNavProgress(
                            filename=fname,
                            bytes_downloaded=downloaded,
                            total_bytes=total_size,
                            speed_bps=speed,
                            elapsed_seconds=elapsed,
                            eta_seconds=eta,
                            message=f"Downloading {fname} ({downloaded / 1024 / 1024:.1f}/{total_size / 1024 / 1024:.1f} MB)",
                            is_complete=(downloaded == total_size),
                        )
                    )

    if temp_file.exists():
        temp_file.replace(final_file)

    return final_file


def download_dynon_databases_only(
    database_url: str = DYNON_NAV_PAGE_URL,
    download_path: Path | str = DEFAULT_DYNON_NAV_DIR,
) -> List[Path]:
    """
    Convenience function matching N890GF tracker's sw_db_updates.py interface.
    Downloads all available .duc database files from Dynon to download_path.
    """
    available = fetch_available_dynon_databases()
    downloaded_paths: List[Path] = []
    dest_dir = Path(download_path)

    for db_info in available:
        saved = download_dynon_database(
            url=db_info["url"],
            dest_dir=dest_dir,
            filename=db_info["filename"],
        )
        downloaded_paths.append(saved)

    return downloaded_paths
