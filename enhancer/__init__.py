"""
FAA Approach Plate Deterministic PDF & PNG Enhancer Package.

Provides strictly non-destructive, vector-and-pixel-preserving RGBA highlight
overlays for FAA Instrument Approach Procedure (IAP) charts using PyMuPDF
vector extraction (`enhancer/pdf_engine.py`), XML-backed d-TPP catalog search
(`enhancer/plate_browser.py`), and fallback OCR/OpenCV for raster images.
"""

from enhancer.ocr_engine import OCREngine, OCRToken, OCRLine
from enhancer.cv_layout import PlateLayout, detect_plate_layout
from enhancer.pdf_engine import load_pdf_plate
from enhancer.rules import HighlightBox, RuleEngine, PALETTE
from enhancer.compositor import (
    export_enhanced_pdf_bytes,
    render_highlights,
    verify_base_pixel_integrity,
)
from enhancer.briefing import generate_5a_briefing
from enhancer.plate_browser import (
    AirmatePlateBrowser,
    DTPPPlateBrowser,
    AirportEntry,
    PlateChartEntry,
    DEFAULT_FAA_PDF_DIR,
    DEFAULT_PLATES_DIR,
)

__all__ = [
    "OCREngine",
    "OCRToken",
    "OCRLine",
    "PlateLayout",
    "detect_plate_layout",
    "load_pdf_plate",
    "HighlightBox",
    "RuleEngine",
    "PALETTE",
    "render_highlights",
    "export_enhanced_pdf_bytes",
    "verify_base_pixel_integrity",
    "generate_5a_briefing",
    "DTPPPlateBrowser",
    "AirmatePlateBrowser",
    "AirportEntry",
    "PlateChartEntry",
    "DEFAULT_FAA_PDF_DIR",
    "DEFAULT_PLATES_DIR",
]
