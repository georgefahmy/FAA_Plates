"""
Deterministic "4-Color Tactical Briefing" Classification Engine (`enhancer/rules.py`).

Evaluates spatial zones, aviation regexes, and OpenCV structural vectors over
grouped OCR lines/tokens and plate geometry to emit non-destructive RGBA
HighlightBoxes, including:
- Angled Magenta flight-path corridors along the Plan View final approach course
- Angled Magenta descent-slope corridors in the Profile View
- Curved/dashed Amber missed-approach escape tracks in the Plan View
- Exact Red MSA circle rings + inner 4-digit MSA altitude callouts
- Clean separation of Missed Approach climb-gradient `*` notes (Red) from
  standard Missed Approach instructions (Amber)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import math
from enhancer.cv_layout import PlateLayout
from enhancer.ocr_engine import OCRLine, OCRToken


def _point_to_seg_dist(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> float:
    dx = x2 - x1
    dy = y2 - y1
    l2 = dx * dx + dy * dy
    if l2 <= 1e-6:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))

# Standardized 4-Color Tactical Briefing RGBA Palette
PALETTE: Dict[str, Tuple[int, int, int, int]] = {
    "CYAN": (0, 215, 245, 80),      # Communications & Setup (Pre-Approach Phase)
    "MAGENTA": (240, 50, 215, 75),  # Primary Flight Path, Fixes & Target Minimums (Active Execution)
    "AMBER": (255, 165, 0, 85),     # Missed Approach & Escape Action (Go-Around Phase)
    "RED": (245, 50, 50, 80),       # "Gotchas," Restrictions, Terrain & Non-Standard Parameters
}

SOLID_BORDER_COLORS: Dict[str, Tuple[int, int, int, int]] = {
    "CYAN": (0, 175, 210, 210),
    "MAGENTA": (210, 25, 185, 210),
    "AMBER": (225, 135, 0, 220),
    "RED": (230, 20, 20, 245),
}

# FAA d-TPP Chart Code Categories
APPROACH_CODES: Set[str] = {"IAP", "IAC", "DAU"}
NON_APPROACH_CODES: Set[str] = {
    "MIN",   # Takeoff and Alternate Minimums booklets
    "APD",   # Airport Diagrams
    "DP",    # Departure Procedures (SIDs)
    "ODP",   # Obstacle Departure Procedures
    "STR",   # Standard Terminal Arrivals (STARs)
    "STAR",  # Alternate STAR code
    "HOT",   # Airport Hot Spots
    "LAH",   # Land and Hold Short Operations
    "AFD",   # Airport/Facility Directory / Chart Supplements
}

NON_APPROACH_TITLE_KEYWORDS: Tuple[str, ...] = (
    "TAKEOFF MINS",
    "IFR TAKEOFF MINIMUMS",
    "TAKEOFF MINIMUMS",
    "TAKEOFF OBSTACLE",
    "ALTERNATE MINS",
    "IFR ALTERNATE AIRPORT MINIMUMS",
    "ALTERNATE AIRPORT MINIMUMS",
    "AIRPORT DIAGRAM",
    "AIRPORT SKETCH",
    "HOT SPOT",
    "AIRPORT HOT SPOTS",
    "DEPARTURE PROCEDURE",
    "OBSTACLE DEPARTURE PROCEDURE",
    "STANDARD TERMINAL ARRIVAL",
    "DIVERSE VECTOR AREA",
    "LAND AND HOLD-SHORT",
    "LAHSO",
    "CHART SUPPLEMENT",
)

APPROACH_TITLE_KEYWORDS: Tuple[str, ...] = (
    "RNAV",
    "ILS",
    "LOC",
    "VOR",
    "NDB",
    "GPS",
    "TACAN",
    "GLS",
    "VISUAL",
    "LDA",
    "SDF",
    "COPTER",
    "HI-VOR",
    "HI-TACAN",
    "HI-ILS",
)

def is_approach_plate(lines: List[OCRLine], chart_code: Optional[str] = None) -> bool:
    """
    Deterministically determine whether a document is an Instrument Approach Procedure (IAP).
    Tactical briefing enhancements (Cyan/Magenta/Amber/Red) are strictly and exclusively
    applied to IAPs/IACs. Non-approach documents (Takeoff/Alternate Minimums, Airport Diagrams,
    SIDs/Departures, STARs/Arrivals, Hot Spots, LAHSO) must NEVER receive tactical highlights.
    """
    if chart_code:
        cc = chart_code.strip().upper()
        if cc in NON_APPROACH_CODES:
            return False
        if cc in APPROACH_CODES:
            # Even if tagged IAP, verify top text doesn't explicitly start with TAKEOFF MINS / AIRPORT DIAGRAM
            top_text = " ".join(ln.text.upper() for ln in lines if ln.norm_y < 0.22)
            if any(kw in top_text for kw in NON_APPROACH_TITLE_KEYWORDS):
                return False
            return True

    # If chart_code is unknown or not provided, inspect lines in the header area (top 22% of page)
    top_lines = [ln for ln in lines if ln.norm_y < 0.22]
    top_text = " ".join(ln.text.upper() for ln in top_lines)

    # Reject if non-approach signature is present
    if any(kw in top_text for kw in NON_APPROACH_TITLE_KEYWORDS):
        return False

    # Check for approach procedure indicators
    has_app_kw = any(kw in top_text for kw in APPROACH_TITLE_KEYWORDS)
    has_rwy = bool(re.search(r"\bRWY\s+\d{1,2}[LCR]?\b|\bRUNWAY\s+\d{1,2}[LCR]?\b", top_text))
    has_strip = any(kw in top_text for kw in ("APP CRS", "TDZE", "APT ELEV", "MISSED APPR", "RWY LDG", "RWY IDG"))

    return (has_app_kw and (has_rwy or has_strip)) or (has_app_kw and "APPROACH" in top_text) or (has_rwy and has_strip)

# Aviation Regex Patterns
VHF_FREQ_RE = re.compile(r"(?<!\d)1[1-3]\d\.\d{1,3}")
UHF_FREQ_RE = re.compile(r"(?<!\d)[2-3]\d{2}\.\d{1,3}")
COURSE_3DIGIT_RE = re.compile(r"\b(\d{3})(?:°|0\b)?")
WAYPOINT_5LTR_RE = re.compile(r"\b([A-Z]{5})\b")
PROFILE_ALT_RE = re.compile(r"\b([1-9]\d{2}0)\b")
SPOT_ELEV_RE = re.compile(r"\b([2-9]\d{3})\b")
HOLD_FIX_EXTRACT_RE = re.compile(r"(?:direct|to)\s+([A-Z]{5})\s+and\s+hold", re.IGNORECASE)
MSA_HEADER_RE = re.compile(r"\bMSA\b|\b25\s*NM\b", re.IGNORECASE)
GP_ANGLE_RE = re.compile(r"\b([2-6]\.\d{2})\s*°?", re.IGNORECASE)

COMMS_FACILITY_KEYWORDS = (
    "AWOS",
    "ASOS",
    "ATIS",
    "APP CON",
    "TOWER",
    "CTAF",
    "UNICOM",
    "GND CON",
    "CLNC",
)

MIN_ROW_HEADERS = (
    "LPV",
    "LNAV/VNAV",
    "LNAV",
    "CIRCLING",
    "ILS",
    "LOC",
)

RED_RESTRICTION_KEYWORDS = (
    "circling na",
    "circling rwy",
    "circling east",
    "circling west",
    "circling north",
    "circling south",
    "procedure na",
    "visibility reduction",
    "baro-vnav",
    "uncompensated",
    "below",
    "helicopter",
    "night",
    "vdp na",
    "sm na",
    "altimeter setting",
    "increase all",
    "offset",
    "autopilot",
    "glideslope",
    "localizer",
)

EXCLUDED_5LETTER_WORDS: Set[str] = {
    "TOWER",
    "LOCAL",
    "FINAL",
    "CLIMB",
    "NORTH",
    "SOUTH",
    "knots",
    "KNOTS",
    "RADAR",
    "CHART",
    "SCALE",
    "MILES",
    "LEVEL",
    "CLEAR",
    "UNTIL",
    "ABOVE",
    "BELOW",
    "NIGHT",
    "PILOT",
    "SPEED",
    "TRACK",
    "CROSS",
    "RIGHT",
    "BOUND",
    "ANGLE",
    "GLIDE",
    "SLOPE",
    "SANTA",
    "SOUND",
    "WATER",
    "FIELD",
    "STATE",
    "POINT",
}


@dataclass
class HighlightBox:
    """Represents a non-destructive highlight annotation on the RGBA overlay."""
    color_key: str  # "CYAN", "MAGENTA", "AMBER", or "RED"
    x_min: int
    y_min: int
    x_max: int
    y_max: int
    label: str
    rule_id: str
    shape: str = "rounded_rect"  # "rounded_rect", "corridor", "dot_path", or "circle"
    border_width: int = 1
    padding: int = 4
    points: Optional[List[Tuple[int, int]]] = None

    @property
    def rgba(self) -> Tuple[int, int, int, int]:
        return PALETTE[self.color_key]

    @property
    def border_rgba(self) -> Tuple[int, int, int, int]:
        return SOLID_BORDER_COLORS[self.color_key]

    @property
    def bbox(self) -> Tuple[int, int, int, int]:
        return (self.x_min, self.y_min, self.x_max, self.y_max)


@dataclass
class ExtractedPlateMetadata:
    """Structured aviation data extracted during deterministic rule evaluation."""
    title: str = "FAA Instrument Approach Plate"
    airport_id: str = ""
    is_approach_plate: bool = True
    document_type: str = "IAP"
    atis_awos_freqs: List[str] = field(default_factory=list)
    approach_freqs: List[str] = field(default_factory=list)
    tower_ctaf_freqs: List[str] = field(default_factory=list)
    gnd_clnc_freqs: List[str] = field(default_factory=list)
    all_comms_groups: List[str] = field(default_factory=list)
    app_crs: Optional[str] = None
    faf_name: Optional[str] = None
    if_name: Optional[str] = None
    waypoints: List[str] = field(default_factory=list)
    profile_altitudes: List[str] = field(default_factory=list)
    minimums_rows: Dict[str, str] = field(default_factory=dict)
    missed_approach_text: List[str] = field(default_factory=list)
    holding_fix: Optional[str] = None
    glidepath_angle: Optional[float] = None
    tch: Optional[str] = None
    msa_altitudes: List[str] = field(default_factory=list)
    red_restrictions: List[str] = field(default_factory=list)
    high_obstacles: List[str] = field(default_factory=list)


class RuleEngine:
    """
    Deterministic 4-Color Tactical Briefing classification engine.
    """

    def __init__(
        self,
        aircraft_categories: Optional[List[str]] = None,
        target_approaches: Optional[List[str]] = None,
        enabled_colors: Optional[Set[str]] = None,
        chart_code: Optional[str] = None,
    ):
        self.aircraft_categories = (
            [c.upper() for c in aircraft_categories]
            if aircraft_categories
            else ["A", "B"]
        )
        self.target_approaches = (
            [a.upper() for a in target_approaches]
            if target_approaches
            else ["LPV", "LNAV/VNAV", "ILS", "LOC", "LNAV"]
        )
        self.enabled_colors = (
            {c.upper() for c in enabled_colors}
            if enabled_colors
            else {"CYAN", "MAGENTA", "AMBER", "RED"}
        )
        self.chart_code = chart_code.upper() if chart_code else None

    def evaluate(
        self,
        tokens: List[OCRToken],
        lines: List[OCRLine],
        layout: PlateLayout,
        chart_code: Optional[str] = None,
    ) -> Tuple[List[HighlightBox], ExtractedPlateMetadata]:
        effective_code = chart_code or self.chart_code
        if not is_approach_plate(lines, chart_code=effective_code):
            meta = ExtractedPlateMetadata()
            meta.is_approach_plate = False
            meta.document_type = effective_code or "STANDARD"
            meta.title = "Standard Document (Non-Approach Chart)"
            return [], meta

        W, H = layout.W, layout.H
        highlights: List[HighlightBox] = []
        meta = ExtractedPlateMetadata()
        meta.is_approach_plate = True

        # Extract plate title if present at very top (y < 0.09 * H) or bottom
        header_lines = [ln for ln in lines if ln.norm_y < 0.09 and ln.norm_x > 0.55]
        if header_lines:
            meta.title = " — ".join(ln.text for ln in header_lines[:2])

        # Populate minimums column bounds from OCR category letters if visible
        self._detect_minimums_category_columns(tokens, layout)

        # Evaluate each of the 4 Tactical Color Rules
        cyan_boxes, comms_line_bboxes = self._evaluate_cyan_comms(tokens, lines, layout, meta)
        comms_top_y = min((b.y_min for b in cyan_boxes), default=int(0.182 * H)) - 2
        magenta_boxes, magenta_bboxes = self._evaluate_magenta_flight_path(tokens, lines, layout, meta)
        amber_boxes, red_missed_boxes = self._evaluate_amber_missed_approach(
            tokens,
            lines,
            layout,
            meta,
            excluded_bboxes=comms_line_bboxes,
            comms_top_y=comms_top_y,
        )
        red_boxes = self._evaluate_red_gotchas(
            tokens,
            lines,
            layout,
            meta,
            extra_red_boxes=red_missed_boxes,
            magenta_bboxes=magenta_bboxes,
            comms_line_bboxes=comms_line_bboxes,
            comms_top_y=comms_top_y,
        )

        all_boxes = cyan_boxes + magenta_boxes + amber_boxes + red_boxes
        highlights = [b for b in all_boxes if b.color_key in self.enabled_colors]
        return highlights, meta

    def _detect_minimums_category_columns(
        self, tokens: List[OCRToken], layout: PlateLayout
    ) -> None:
        """Locate CATEGORY A / B / C / D column headers in the Minimums table."""
        if layout.minimums_col_bounds:
            return
        W, H = layout.W, layout.H
        cat_tokens: Dict[str, OCRToken] = {}
        for tok in tokens:
            if 0.74 <= tok.norm_y <= 0.885 and 0.15 <= tok.norm_x <= 0.95:
                clean = tok.text.strip().upper()
                if clean in ("A", "B", "C", "D") and clean not in cat_tokens:
                    cat_tokens[clean] = tok

        if len(cat_tokens) >= 2:
            sorted_cats = sorted(cat_tokens.items(), key=lambda kv: kv[1].center_x)
            col_w = int(0.11 * W)
            if len(sorted_cats) >= 2:
                diffs = [
                    sorted_cats[i + 1][1].center_x - sorted_cats[i][1].center_x
                    for i in range(len(sorted_cats) - 1)
                ]
                col_w = max(25, int(sum(diffs) / len(diffs)))
            for cat_letter, tok in sorted_cats:
                cx = int(tok.center_x)
                layout.minimums_col_bounds[cat_letter] = (
                    cx - col_w // 2,
                    cx + col_w // 2,
                )

    # -------------------------------------------------------------------------
    # A. CYAN Rules (Communications Strip: 0.10 * H < y < 0.225 * H)
    # -------------------------------------------------------------------------
    def _evaluate_cyan_comms(
        self,
        tokens: List[OCRToken],
        lines: List[OCRLine],
        layout: PlateLayout,
        meta: ExtractedPlateMetadata,
    ) -> Tuple[List[HighlightBox], Set[Tuple[int, int, int, int]]]:
        W, H = layout.W, layout.H
        comms_lines = [
            ln for ln in lines if 0.10 * H <= ln.center_y <= 0.225 * H
        ]

        facility_lines: List[OCRLine] = []
        freq_lines: List[OCRLine] = []
        comms_line_bboxes: Set[Tuple[int, int, int, int]] = set()

        for ln in comms_lines:
            # Skip prose restriction lines (e.g. "When control tower closed, use Mineta San...")
            low_ln = ln.text.lower()
            if any(
                w in low_ln
                for w in ("night", "circling", "altimeter", "closed", "visibility", "increase", "missed")
            ):
                continue

            sub_lines: List[OCRLine] = []
            if len(ln.tokens) > 1:
                toks_sorted = sorted(ln.tokens, key=lambda t: t.x_min)
                cur_toks = [toks_sorted[0]]
                for tok in toks_sorted[1:]:
                    prev = cur_toks[-1]
                    crossed = any(
                        prev.x_max - 2 <= vx <= tok.x_min + 2
                        for vx in layout.vertical_lines_x
                    )
                    cur_str = " ".join(t.text.upper() for t in cur_toks)
                    tok_up = tok.text.upper()
                    starts_new_fac = (
                        any(kw in cur_str for kw in COMMS_FACILITY_KEYWORDS)
                        and tok_up in ("ATIS", "AWOS", "ASOS", "GND", "CLNC", "UNICOM", "CTAF")
                    )
                    if crossed or starts_new_fac:
                        sub_lines.append(OCRLine(tokens=cur_toks, W=W, H=H))
                        cur_toks = [tok]
                    else:
                        cur_toks.append(tok)
                if cur_toks:
                    sub_lines.append(OCRLine(tokens=cur_toks, W=W, H=H))
            else:
                sub_lines = [ln]

            for sub_ln in sub_lines:
                sub_raw = sub_ln.text
                sub_up = sub_raw.upper()
                has_freq = bool(VHF_FREQ_RE.search(sub_up) or UHF_FREQ_RE.search(sub_up))
                # Facility headers on FAA plates are ALWAYS uppercase (e.g. TOWER, ATIS, APP CON)
                matched_kws = [kw for kw in COMMS_FACILITY_KEYWORDS if kw in sub_raw]
                if has_freq:
                    freq_lines.append(sub_ln)
                    comms_line_bboxes.add(sub_ln.bbox)
                    # If the line is a frequency line whose only keyword is (CTAF), keep it in freq_lines only
                    # so it pairs cleanly with the TOWER header above it without creating a duplicate box
                    if matched_kws and matched_kws != ["CTAF"]:
                        facility_lines.append(sub_ln)
                elif matched_kws:
                    facility_lines.append(sub_ln)
                    comms_line_bboxes.add(sub_ln.bbox)

        paired_freq_ids: Set[int] = set()
        cyan_boxes: List[HighlightBox] = []

        for fac_ln in facility_lines:
            x1, y1, x2, y2 = fac_ln.bbox
            group_texts = [fac_ln.text]
            fac_has_own_freq = bool(
                VHF_FREQ_RE.search(fac_ln.text) or UHF_FREQ_RE.search(fac_ln.text)
            )

            best_idx: Optional[int] = None
            best_dist = float("inf")
            for idx, fq_ln in enumerate(freq_lines):
                if fq_ln is fac_ln:
                    paired_freq_ids.add(idx)
                    continue
                vert_diff = fq_ln.center_y - fac_ln.center_y
                if not (0 <= vert_diff <= 0.055 * H):
                    continue
                min_cx = min(fac_ln.center_x, fq_ln.center_x)
                max_cx = max(fac_ln.center_x, fq_ln.center_x)
                crossed_vert = any(
                    min_cx + 4 < vx < max_cx - 4 for vx in layout.vertical_lines_x
                )
                if crossed_vert:
                    continue
                horiz_dist = abs(fac_ln.center_x - fq_ln.center_x)
                if horiz_dist <= 0.085 * W and horiz_dist < best_dist:
                    best_dist = horiz_dist
                    best_idx = idx

            if best_idx is not None:
                fq_ln = freq_lines[best_idx]
                x1 = min(x1, fq_ln.x_min)
                y1 = min(y1, fq_ln.y_min)
                x2 = max(x2, fq_ln.x_max)
                y2 = max(y2, fq_ln.y_max)
                group_texts.append(fq_ln.text)
                paired_freq_ids.add(best_idx)
            elif not fac_has_own_freq:
                # Never highlight a facility keyword that has no associated radio frequency
                continue

            combined_label = " ".join(group_texts)
            meta.all_comms_groups.append(combined_label)
            self._categorize_comms_for_briefing(combined_label, meta)

            cyan_boxes.append(
                HighlightBox(
                    color_key="CYAN",
                    x_min=x1,
                    y_min=y1,
                    x_max=x2,
                    y_max=y2,
                    label=f"Comms: {combined_label}",
                    rule_id="CYAN_COMMS_GROUP",
                    padding=5,
                )
            )

        for idx, fq_ln in enumerate(freq_lines):
            if idx not in paired_freq_ids:
                meta.all_comms_groups.append(fq_ln.text)
                self._categorize_comms_for_briefing(fq_ln.text, meta)
                cyan_boxes.append(
                    HighlightBox(
                        color_key="CYAN",
                        x_min=fq_ln.x_min,
                        y_min=fq_ln.y_min,
                        x_max=fq_ln.x_max,
                        y_max=fq_ln.y_max,
                        label=f"Freq: {fq_ln.text}",
                        rule_id="CYAN_COMMS_FREQ",
                        padding=5,
                    )
                )

        return cyan_boxes, comms_line_bboxes

    def _categorize_comms_for_briefing(
        self, text: str, meta: ExtractedPlateMetadata
    ) -> None:
        upper = text.upper()
        freqs = VHF_FREQ_RE.findall(upper) + UHF_FREQ_RE.findall(upper)
        entry = f"{text}" if not freqs else f"{text} ({', '.join(freqs)})"
        if any(k in upper for k in ("ATIS", "AWOS", "ASOS")):
            meta.atis_awos_freqs.append(entry)
        elif "APP" in upper:
            meta.approach_freqs.append(entry)
        elif any(k in upper for k in ("TOWER", "CTAF", "UNICOM")):
            meta.tower_ctaf_freqs.append(entry)
        elif any(k in upper for k in ("GND", "CLNC")):
            meta.gnd_clnc_freqs.append(entry)

    # -------------------------------------------------------------------------
    # B. MAGENTA Rules (Course, Fixes, Flight-Path Corridors & Minimums)
    # -------------------------------------------------------------------------
    def _evaluate_magenta_flight_path(
        self,
        tokens: List[OCRToken],
        lines: List[OCRLine],
        layout: PlateLayout,
        meta: ExtractedPlateMetadata,
    ) -> Tuple[List[HighlightBox], Set[Tuple[int, int, int, int]]]:
        W, H = layout.W, layout.H
        magenta_boxes: List[HighlightBox] = []
        magenta_bboxes: Set[Tuple[int, int, int, int]] = set()

        # 1. Approach Course Box (Top Left Briefing Strip: y < 0.12 * H, x < 0.30 * W)
        top_left_toks = [
            t for t in tokens if t.center_y < 0.12 * H and t.center_x < 0.30 * W
        ]
        crs_header_toks = [
            t for t in top_left_toks if t.text.upper() in ("APP", "CRS", "APPCRS")
        ]

        app_crs_val: Optional[str] = None
        crs_value_tok: Optional[OCRToken] = None
        best_score = float("inf")

        header_cx = (
            sum(t.center_x for t in crs_header_toks) / len(crs_header_toks)
            if crs_header_toks
            else 0.15 * W
        )
        header_y_min = (
            min(t.y_min for t in crs_header_toks) if crs_header_toks else 0
        )

        for tok in top_left_toks:
            t_up = tok.text.upper().strip()
            if t_up in ("APP", "CRS", "WAAS", "CH", "TDZE", "APT", "ELEV", "RWY", "LDG", "IDG"):
                continue
            m_deg = re.match(r"^(\d{3})(?:°|0)?$", t_up)
            if m_deg:
                cand = m_deg.group(1)
                if 1 <= int(cand) <= 360 and tok.center_y >= header_y_min:
                    horiz_dist = abs(tok.center_x - header_cx)
                    if horiz_dist <= 0.08 * W:
                        score = horiz_dist - (40.0 if ("°" in t_up or len(t_up) == 4) else 0.0)
                        if score < best_score:
                            best_score = score
                            app_crs_val = cand
                            crs_value_tok = tok

        if app_crs_val and crs_value_tok is not None:
            # Highlight tightly around the 3-digit course value (and APP CRS header)
            # without bleeding into the adjacent WAAS or Rwy Ldg columns
            box_toks = [crs_value_tok] + [
                ht for ht in crs_header_toks if abs(ht.center_x - crs_value_tok.center_x) <= 0.055 * W
            ]
            x1 = min(t.x_min for t in box_toks)
            y1 = min(t.y_min for t in box_toks)
            x2 = max(t.x_max for t in box_toks)
            y2 = max(t.y_max for t in box_toks)
            magenta_boxes.append(
                HighlightBox(
                    color_key="MAGENTA",
                    x_min=x1,
                    y_min=y1,
                    x_max=x2,
                    y_max=y2,
                    label=f"APP CRS {app_crs_val}°",
                    rule_id="MAGENTA_APP_CRS_BOX",
                    padding=4,
                )
            )

        meta.app_crs = app_crs_val
        holding_fix_name = self._find_holding_fix_name(lines, W, H)

        # 2a. Plan View (0.20 * H < y < 0.65 * H):
        # First pass: collect course anchors and (FAF)/(IF) anchor lines so we can detect
        # the Plan View flight path corridors (`plan_segs`) before deciding which text boxes to draw.
        plan_lines = [
            ln for ln in lines if 0.20 * H < ln.center_y < 0.65 * H
        ]
        faf_if_anchor_lines: List[OCRLine] = []
        course_anchors: List[Tuple[float, float]] = []

        def _is_excluded_plan_note(ln_obj: OCRLine) -> bool:
            u_txt = ln_obj.text.upper()
            l_txt = ln_obj.text.lower()
            return (
                "procedure na" in l_txt
                or "northwest" in l_txt
                or "southeast" in l_txt
                or "eastbound" in l_txt
                or "westbound" in l_txt
                or "radials" in l_txt
                or "vortac" in l_txt
                or "MSA" in u_txt
            )

        for ln in plan_lines:
            if _is_excluded_plan_note(ln):
                continue
            upper_txt = ln.text.upper()
            has_faf = any(k in upper_txt for k in ("(FAF)", "FAF)", "(FAF"))
            has_if = any(k in upper_txt for k in ("(IF)", "IF)", "(IF"))
            has_rwy = bool(re.search(r"\bRW\d{2}[LRC]?\b", upper_txt))
            has_crs = bool(
                app_crs_val and re.search(rf"\b{re.escape(app_crs_val)}°?\b", upper_txt)
            )
            if has_rwy or has_faf or has_if or has_crs:
                course_anchors.append((ln.center_x, ln.center_y))
            if has_faf or has_if:
                faf_if_anchor_lines.append(ln)

        # Detect and render all connected Plan View Final/Intermediate Approach Course Corridors!
        plan_segs = layout.detect_plan_course_segments(course_anchors)
        for px1, py1, px2, py2 in reversed(plan_segs):
            magenta_boxes.insert(
                0,
                HighlightBox(
                    color_key="MAGENTA",
                    x_min=min(px1, px2),
                    y_min=min(py1, py2),
                    x_max=max(px1, px2),
                    y_max=max(py1, py2),
                    label=f"Plan View Approach Course Path ({app_crs_val or ''}°)",
                    rule_id="MAGENTA_PLAN_COURSE_PATH",
                    shape="corridor",
                    padding=max(10, int(round(0.011 * W))),
                    points=[(px1, py1), (px2, py2)],
                ),
            )

        def _is_on_plan_segs(cx: float, cy: float, tol_factor: float = 0.036) -> bool:
            return bool(plan_segs) and any(
                _point_to_seg_dist(cx, cy, sx1, sy1, sx2, sy2) <= tol_factor * W
                for (sx1, sy1, sx2, sy2) in plan_segs
            )

        # Second pass: highlight active fixes ((FAF), (IF), primary 5-letter waypoints)
        # while skipping redundant overlapping boxes on numbers (like 321° or 2100) that
        # sit directly on an already-highlighted `plan_segs` corridor ribbon.
        faf_if_box_map: Dict[Tuple[int, int, int, int], HighlightBox] = {}
        for ln in plan_lines:
            if _is_excluded_plan_note(ln):
                continue
            upper_txt = ln.text.upper()
            has_faf = any(k in upper_txt for k in ("(FAF)", "FAF)", "(FAF"))
            has_if = any(k in upper_txt for k in ("(IF)", "IF)", "(IF"))
            has_iaf = "(IAF)" in upper_txt
            has_crs = bool(
                app_crs_val and re.search(rf"\b{re.escape(app_crs_val)}°?\b", upper_txt)
            )
            wp_matches = [
                w
                for w in WAYPOINT_5LTR_RE.findall(upper_txt)
                if w not in EXCLUDED_5LETTER_WORDS
            ]

            if has_faf and wp_matches and not meta.faf_name:
                meta.faf_name = wp_matches[0]
            if has_if and wp_matches and not meta.if_name:
                meta.if_name = wp_matches[0]

            matched_anchor: Optional[OCRLine] = None
            is_primary_fix = has_faf or has_if or has_crs
            if not is_primary_fix and wp_matches and not has_iaf:
                # Check if this 5-letter waypoint is immediately below a (FAF) or (IF) label
                for prev_ln in faf_if_anchor_lines:
                    if (
                        0 <= ln.center_y - prev_ln.center_y <= 0.035 * H
                        and abs(ln.center_x - prev_ln.center_x) <= 0.06 * W
                    ):
                        is_primary_fix = True
                        matched_anchor = prev_ln
                        if "FAF" in prev_ln.text.upper() and not meta.faf_name:
                            meta.faf_name = wp_matches[0]
                        if "IF" in prev_ln.text.upper() and not meta.if_name:
                            meta.if_name = wp_matches[0]
                        break
                # Or if it's on the final approach course (not a distant IAF)
                if not is_primary_fix and wp_matches[0] != holding_fix_name:
                    iaf_above = any(
                        "(IAF)" in o.text.upper()
                        and 0 <= ln.center_y - o.center_y <= 0.035 * H
                        and abs(ln.center_x - o.center_x) <= 0.06 * W
                        for o in plan_lines
                    )
                    if not iaf_above and ln.center_y < 0.58 * H:
                        is_primary_fix = True

            if is_primary_fix:
                for w in wp_matches:
                    if w not in meta.waypoints:
                        meta.waypoints.append(w)
                magenta_bboxes.add(ln.bbox)

                # If this line is just the course number (e.g. "321°") sitting on the already-highlighted
                # Plan View path ribbon, do NOT draw a second overlapping box around the numbers.
                if has_crs and not has_faf and not has_if and not wp_matches and _is_on_plan_segs(ln.center_x, ln.center_y):
                    continue

                # If this line is the Missed Approach Holding Fix (e.g. ACETY), let the Amber rule
                # highlight the fix name so it doesn't get double-highlighted in both Magenta and Amber.
                if wp_matches and all(w == holding_fix_name for w in wp_matches) and not has_faf and not has_if:
                    continue

                # If this 5-letter fix is directly below its (FAF)/(IF) label, merge them into a single clean box
                if matched_anchor is not None and matched_anchor.bbox in faf_if_box_map and not ln.is_rotated:
                    parent_box = faf_if_box_map[matched_anchor.bbox]
                    parent_box.x_min = min(parent_box.x_min, ln.x_min)
                    parent_box.y_min = min(parent_box.y_min, ln.y_min)
                    parent_box.x_max = max(parent_box.x_max, ln.x_max)
                    parent_box.y_max = max(parent_box.y_max, ln.y_max)
                    parent_box.label = f"Plan Fix: {matched_anchor.text} {ln.text}"
                    continue

                hb = HighlightBox(
                    color_key="MAGENTA",
                    x_min=ln.x_min,
                    y_min=ln.y_min,
                    x_max=ln.x_max,
                    y_max=ln.y_max,
                    label=f"Plan Fix/Course: {ln.text}",
                    rule_id="MAGENTA_PLAN_FIX_CRS",
                    shape="quad" if (ln.is_rotated and ln.quad_points) else "rounded_rect",
                    padding=3,
                    points=ln.quad_points if (ln.is_rotated and ln.quad_points) else None,
                )
                magenta_boxes.append(hb)
                if has_faf or has_if:
                    faf_if_box_map[ln.bbox] = hb

        # Record any rotated 3-digit intermediate course label on plan_segs in magenta_bboxes
        # without adding a second overlapping box on top of the path ribbon
        for ln in plan_lines:
            if ln.bbox in magenta_bboxes:
                continue
            if ln.is_rotated and ln.quad_points and re.match(r"^\d{3}°?$", ln.text.strip()):
                if _is_on_plan_segs(ln.center_x, ln.center_y, tol_factor=0.030):
                    magenta_bboxes.add(ln.bbox)

        # Highlight 4-digit altitudes adjacent to (FAF) or (IF) in the Plan View ONLY when
        # not already sitting along the highlighted Plan View path ribbon
        for ln in plan_lines:
            if ln.bbox in magenta_bboxes:
                continue
            m_alt = PROFILE_ALT_RE.search(ln.text)
            if m_alt:
                alt_str = m_alt.group(1)
                if ln.is_rotated and alt_str.endswith("000") and "*" not in ln.text:
                    if not _is_on_plan_segs(ln.center_x, ln.center_y, tol_factor=0.032):
                        continue
                for anchor in faf_if_anchor_lines:
                    if (
                        abs(ln.center_x - anchor.center_x) <= 0.09 * W
                        and abs(ln.center_y - anchor.center_y) <= 0.045 * H
                    ):
                        magenta_bboxes.add(ln.bbox)
                        # If the path ribbon is already highlighting this segment, skip the overlapping number box
                        if _is_on_plan_segs(ln.center_x, ln.center_y, tol_factor=0.038):
                            break
                        magenta_boxes.append(
                            HighlightBox(
                                color_key="MAGENTA",
                                x_min=ln.x_min,
                                y_min=ln.y_min,
                                x_max=ln.x_max,
                                y_max=ln.y_max,
                                label=f"Fix Altitude: {alt_str}",
                                rule_id="MAGENTA_PLAN_FIX_ALT",
                                shape="quad" if (ln.is_rotated and ln.quad_points) else "rounded_rect",
                                padding=3,
                                points=ln.quad_points if (ln.is_rotated and ln.quad_points) else None,
                            )
                        )
                        break

        # 2b. Profile View (between prof_top_y and the CATEGORY row header):
        # Determine whether the Airport Sketch is on the Left (e.g. RHV) or Right (e.g. E16, KSMO)
        is_left_sketch = (
            "CATEGORY" in layout.minimums_col_bounds
            and layout.minimums_col_bounds["CATEGORY"][0] > 0.22 * W
        )
        prof_x_min = (
            layout.minimums_col_bounds["CATEGORY"][0] - 4
            if is_left_sketch
            else int(0.04 * W)
        )
        prof_x_max = int(0.96 * W) if is_left_sketch else int(0.665 * W)
        prof_top_y = (
            min(b[1] for b in layout.profile_icon_boxes) - 3
            if layout.profile_icon_boxes
            else int(0.635 * H)
        )
        cat_header_y = min(
            (ln.y_min for ln in lines if "CATEGORY" in ln.text.upper() and ln.center_y > 0.74 * H),
            default=int(0.785 * H),
        )

        # Detect the Profile View Descent Slope Corridor first
        prof_seg = layout.detect_profile_descent_segment(
            min_x_hint=prof_x_min + int(0.08 * W),
            max_x_hint=prof_x_max - int(0.02 * W),
        )

        profile_lines = [
            ln
            for ln in lines
            if prof_top_y < ln.center_y < (cat_header_y - 10)
            and prof_x_min <= ln.center_x <= prof_x_max
        ]
        prof_alt_xs: List[int] = []
        prof_alt_boxes: List[HighlightBox] = []

        for ln in profile_lines:
            upper_txt = ln.text.upper()
            # Exclude top-left missed approach icon boxes
            in_icon_box = any(
                bx0 - 6 <= ln.center_x <= bx1 + 6 and by0 - 6 <= ln.center_y <= by1 + 6
                for (bx0, by0, bx1, by1) in layout.profile_icon_boxes
            )
            if in_icon_box or (
                ln.center_y < prof_top_y + 0.055 * H
                and ln.center_x < prof_x_min + 0.22 * W
            ):
                continue
            if any(k in upper_txt for k in ("GP", "TCH", "VGSI", "GLIDEPATH", "NM", "UP", "DOWN", "X ")):
                continue

            has_crs = bool(
                app_crs_val and re.search(rf"\b{re.escape(app_crs_val)}°?\b", upper_txt)
            )
            if (
                not has_crs
                and ln.is_rotated
                and prof_seg is not None
                and re.match(r"^\d{3}°?$", upper_txt.strip())
            ):
                if (
                    _point_to_seg_dist(
                        ln.center_x, ln.center_y, prof_seg[0], prof_seg[1], prof_seg[2], prof_seg[3]
                    )
                    <= 0.028 * W
                ):
                    has_crs = True

            alt_matches = PROFILE_ALT_RE.findall(upper_txt)
            if has_crs or alt_matches:
                for alt in alt_matches:
                    if alt not in meta.profile_altitudes:
                        meta.profile_altitudes.append(alt)
                if ln.is_rotated and ln.quad_points:
                    prof_alt_xs.append(int(ln.center_x))
                    magenta_bboxes.add(ln.bbox)
                    # Only draw a separate box for rotated course text if no slope ribbon was detected
                    if prof_seg is None:
                        magenta_boxes.append(
                            HighlightBox(
                                color_key="MAGENTA",
                                x_min=ln.x_min,
                                y_min=ln.y_min,
                                x_max=ln.x_max,
                                y_max=ln.y_max,
                                label=f"Profile Course/Alt: {ln.text}",
                                rule_id="MAGENTA_PROFILE_CRS_ALT",
                                shape="quad",
                                padding=3,
                                points=ln.quad_points,
                            )
                        )
                else:
                    for tok in ln.tokens:
                        t_up = tok.text.upper()
                        t_is_crs = bool(
                            app_crs_val and re.search(rf"\b{re.escape(app_crs_val)}°?\b", t_up)
                        )
                        t_is_alt = bool(PROFILE_ALT_RE.search(t_up))
                        if t_is_crs or t_is_alt:
                            prof_alt_xs.append(int(tok.center_x))
                            magenta_bboxes.add(tok.bbox)
                            # Skip course number box if the slope ribbon already highlights the path
                            if t_is_crs and not t_is_alt and prof_seg is not None:
                                continue
                            hb_alt = HighlightBox(
                                color_key="MAGENTA",
                                x_min=tok.x_min,
                                y_min=tok.y_min,
                                x_max=tok.x_max,
                                y_max=tok.y_max,
                                label=f"Profile Course/Alt: {tok.text}",
                                rule_id="MAGENTA_PROFILE_CRS_ALT",
                                shape="quad" if (tok.is_rotated and tok.quad_points) else "rounded_rect",
                                padding=3,
                                points=tok.quad_points if (tok.is_rotated and tok.quad_points) else None,
                            )
                            prof_alt_boxes.append(hb_alt)
                            magenta_boxes.append(hb_alt)

        if prof_seg is None and prof_alt_xs:
            min_x_hint = min(prof_alt_xs) - int(0.06 * W)
            max_x_hint = max(prof_alt_xs) + int(0.02 * W)
            prof_seg = layout.detect_profile_descent_segment(
                min_x_hint=min_x_hint, max_x_hint=max_x_hint
            )
        if prof_seg is not None:
            sx1, sy1, sx2, sy2 = prof_seg
            slope_pad = max(9, int(round(0.0095 * W)))
            # Trim the outer end of the slope ribbon slightly if a horizontal crossing altitude box
            # sits immediately adjacent to that endpoint, preventing overlapping rounded caps.
            seg_dx = float(sx2 - sx1)
            seg_dy = float(sy2 - sy1)
            seg_len = math.hypot(seg_dx, seg_dy)
            if seg_len > 25.0:
                ux, uy = seg_dx / seg_len, seg_dy / seg_len
                for ab in prof_alt_boxes:
                    # Check right endpoint (sx2, sy2) against left edge of altitude box
                    if abs(sx2 - ab.x_min) <= slope_pad + 6 and (ab.y_min - slope_pad - 6) <= sy2 <= (ab.y_max + slope_pad + 6):
                        trim = max(0.0, (sx2 + slope_pad + 4) - (ab.x_min - ab.padding))
                        if 0 < trim < seg_len * 0.25:
                            sx2 = int(round(sx2 - ux * trim))
                            sy2 = int(round(sy2 - uy * trim))
                    # Check left endpoint (sx1, sy1) against right edge of altitude box
                    if abs(sx1 - ab.x_max) <= slope_pad + 6 and (ab.y_min - slope_pad - 6) <= sy1 <= (ab.y_max + slope_pad + 6):
                        trim = max(0.0, (ab.x_max + ab.padding) - (sx1 - slope_pad - 4))
                        if 0 < trim < seg_len * 0.25:
                            sx1 = int(round(sx1 + ux * trim))
                            sy1 = int(round(sy1 + uy * trim))

            magenta_boxes.insert(
                0,
                HighlightBox(
                    color_key="MAGENTA",
                    x_min=min(sx1, sx2),
                    y_min=min(sy1, sy2),
                    x_max=max(sx1, sx2),
                    y_max=max(sy1, sy2),
                    label=f"Profile Descent Slope Path ({app_crs_val or ''}°)",
                    rule_id="MAGENTA_PROFILE_SLOPE_PATH",
                    shape="corridor",
                    padding=slope_pad,
                    points=[(sx1, sy1), (sx2, sy2)],
                ),
            )

        # 3. Minimums Table (below CATEGORY header within [prof_x_min, prof_x_max])
        min_lines = [
            ln
            for ln in lines
            if ln.center_y > cat_header_y and prof_x_min <= ln.center_x <= prof_x_max
        ]
        candidate_min_rows: List[Tuple[str, OCRLine]] = []
        for ln in min_lines:
            if ln.x_min > prof_x_min + 0.19 * W:
                continue
            upper_txt = ln.text.upper()
            matched_header: Optional[str] = None
            if re.search(r"LNAV\s*[/\\I1|]?\s*VNAV", upper_txt) or "VNAV" in upper_txt:
                matched_header = "LNAV/VNAV"
            else:
                for hdr in MIN_ROW_HEADERS:
                    if hdr == "LNAV/VNAV":
                        continue
                    if re.search(rf"\b{re.escape(hdr)}\b", upper_txt):
                        matched_header = hdr
                        break

            if matched_header:
                row_band_lines = [
                    other
                    for other in min_lines
                    if abs(other.center_y - ln.center_y) <= max(8, 0.012 * H)
                ]
                full_row_str = " | ".join(
                    r.text for r in sorted(row_band_lines, key=lambda x: x.x_min)
                )
                meta.minimums_rows[matched_header] = full_row_str
                candidate_min_rows.append((matched_header, ln))

        # Highlight target approach rows; if none of target_approaches exist on this plate
        # (e.g. an LNAV-only plate when defaults are LPV + LNAV/VNAV), fall back to the primary non-circling row
        rows_to_highlight = [
            (hdr, ln) for (hdr, ln) in candidate_min_rows if hdr in self.target_approaches
        ]
        if not rows_to_highlight and candidate_min_rows:
            non_circling = [(hdr, ln) for (hdr, ln) in candidate_min_rows if hdr != "CIRCLING"]
            rows_to_highlight = non_circling[:1] if non_circling else candidate_min_rows[:1]

        for matched_header, ln in rows_to_highlight:
            row_bbox = layout.snap_minimums_row(
                row_y_center=ln.center_y,
                row_y_min=ln.y_min,
                row_y_max=ln.y_max,
                categories=self.aircraft_categories,
            )
            magenta_boxes.append(
                HighlightBox(
                    color_key="MAGENTA",
                    x_min=row_bbox[0],
                    y_min=row_bbox[1],
                    x_max=row_bbox[2],
                    y_max=row_bbox[3],
                    label=f"Minimums Row ({matched_header} Cat {'/'.join(self.aircraft_categories)})",
                    rule_id="MAGENTA_MINIMUMS_ROW",
                    padding=2,
                )
            )

        return magenta_boxes, magenta_bboxes

    def _find_holding_fix_name(
        self, lines: List[OCRLine], W: int, H: int
    ) -> Optional[str]:
        missed_zone_lines = [
            ln for ln in lines if 0.08 * H <= ln.center_y <= 0.19 * H and ln.center_x >= 0.48 * W
        ]
        combined_text = " ".join(ln.text for ln in missed_zone_lines)
        m = HOLD_FIX_EXTRACT_RE.search(combined_text)
        if m:
            return m.group(1).upper()
        # Fallback: check any 5-letter uppercase fix in the Missed Approach text box
        for ln in missed_zone_lines:
            if "MISSED" in ln.text.upper() or "HOLD" in ln.text.upper():
                for w in WAYPOINT_5LTR_RE.findall(ln.text.upper()):
                    if w not in EXCLUDED_5LETTER_WORDS and w != "APPRO":
                        return w
        return None

    # -------------------------------------------------------------------------
    # C. AMBER Rules (Missed Approach Text, Icons, Escape Track & Holding Fix)
    # -------------------------------------------------------------------------
    def _evaluate_amber_missed_approach(
        self,
        tokens: List[OCRToken],
        lines: List[OCRLine],
        layout: PlateLayout,
        meta: ExtractedPlateMetadata,
        excluded_bboxes: Optional[Set[Tuple[int, int, int, int]]] = None,
        comms_top_y: Optional[int] = None,
    ) -> Tuple[List[HighlightBox], List[HighlightBox]]:
        W, H = layout.W, layout.H
        amber_boxes: List[HighlightBox] = []
        red_from_missed: List[HighlightBox] = []
        excl = excluded_bboxes or set()
        max_briefing_y = min(int(0.180 * H), comms_top_y) if comms_top_y is not None else int(0.180 * H)

        # 1. Textual Missed Approach Box:
        # Locate the exact top-left anchor of the "MISSED APPROACH:" header so we never
        # bleed leftward into wide T/A Notes boxes (such as on RHV where T/A Notes span to x=0.644*W)
        ma_header_toks = [
            t
            for t in tokens
            if 0.105 * H < t.center_y < max_briefing_y
            and t.center_x > 0.45 * W
            and t.text.upper().rstrip(":") in ("MISSED", "APPROACH")
        ]
        if ma_header_toks:
            ma_left_x = min(t.x_min for t in ma_header_toks) - 6
            ma_top_y = min(t.y_min for t in ma_header_toks) - 4
        else:
            ma_left_x = int(0.55 * W)
            ma_top_y = int(0.108 * H)

        missed_toks = [
            t
            for t in tokens
            if ma_top_y <= t.center_y < max_briefing_y
            and t.x_min >= ma_left_x
            and not any(kw in t.text for kw in COMMS_FACILITY_KEYWORDS)
        ]

        # Bucket missed_toks tightly by single text line (tolerance ~0.45 * token height)
        missed_rows: List[List[OCRToken]] = []
        for tok in sorted(missed_toks, key=lambda t: (t.center_y, t.x_min)):
            placed = False
            for row in missed_rows:
                avg_y = sum(r.center_y for r in row) / len(row)
                avg_h = sum(r.height for r in row) / len(row)
                if abs(tok.center_y - avg_y) <= max(4.5, avg_h * 0.45):
                    row.append(tok)
                    placed = True
                    break
            if not placed:
                missed_rows.append([tok])

        red_mode_triggered = False
        for row in sorted(missed_rows, key=lambda r: min(t.y_min for t in r)):
            row_sorted = sorted(row, key=lambda t: t.x_min)
            row_text = " ".join(t.text for t in row_sorted).strip()
            low = row_text.lower()
            meta.missed_approach_text.append(row_text)

            # Once a line in the Missed Approach box starts with `*` or contains
            # `minimum climb` / `feet per NM`, that line and any continuation line
            # of that restriction are highlighted in RED!
            if (
                row_text.startswith("*")
                or "*missed" in low
                or "minimum climb" in low
                or "feet per nm" in low
            ):
                red_mode_triggered = True

            rx1 = min(t.x_min for t in row_sorted)
            ry1 = min(t.y_min for t in row_sorted)
            rx2 = max(t.x_max for t in row_sorted)
            ry2 = max(t.y_max for t in row_sorted)

            if red_mode_triggered:
                meta.red_restrictions.append(row_text)
                red_from_missed.append(
                    HighlightBox(
                        color_key="RED",
                        x_min=rx1,
                        y_min=ry1,
                        x_max=rx2,
                        y_max=ry2,
                        label=f"Climb Gradient Restriction: {row_text}",
                        rule_id="RED_CLIMB_GRADIENT",
                        padding=3,
                    )
                )
            else:
                amber_boxes.append(
                    HighlightBox(
                        color_key="AMBER",
                        x_min=rx1,
                        y_min=ry1,
                        x_max=rx2,
                        y_max=ry2,
                        label=f"Missed Approach: {row_text}",
                        rule_id="AMBER_MISSED_TEXT",
                        padding=3,
                    )
                )

        holding_fix = self._find_holding_fix_name(lines, W, H)
        meta.holding_fix = holding_fix

        # 2. Profile View Missed Approach Icons
        if layout.profile_icon_boxes:
            for idx, box in enumerate(layout.profile_icon_boxes):
                amber_boxes.append(
                    HighlightBox(
                        color_key="AMBER",
                        x_min=box[0] + 2,
                        y_min=box[1] + 2,
                        x_max=box[2] - 2,
                        y_max=box[3] - 2,
                        label=f"Profile Missed Apch Icon #{idx + 1}",
                        rule_id="AMBER_PROFILE_ICON",
                        padding=1,
                    )
                )
        else:
            icon_zone_lines = [
                ln
                for ln in lines
                if 0.62 * H < ln.center_y < 0.73 * H and ln.center_x < 0.35 * W
            ]
            for ln in icon_zone_lines:
                amber_boxes.append(
                    HighlightBox(
                        color_key="AMBER",
                        x_min=ln.x_min,
                        y_min=ln.y_min,
                        x_max=ln.x_max,
                        y_max=ln.y_max,
                        label=f"Profile Missed Apch Icon: {ln.text}",
                        rule_id="AMBER_PROFILE_ICON",
                        padding=4,
                    )
                )

        # 3. Plan View & Profile View Dashed Missed-Approach Escape Tracks & Holding Fix
        prof_top_y = (
            min(b[1] for b in layout.profile_icon_boxes) - 3
            if layout.profile_icon_boxes
            else int(0.645 * H)
        )
        plan_lines = [
            ln for ln in lines if 0.20 * H < ln.center_y < prof_top_y
        ]
        rwy_point: Optional[Tuple[float, float]] = None
        for ln in plan_lines:
            upper_txt = ln.text.upper()
            if re.search(r"\bRW\d{2}[LRC]?\b", upper_txt):
                rwy_point = (ln.center_x, ln.center_y)
            has_missed_label = "MISSED APCH FIX" in upper_txt
            has_hold_fix = bool(
                holding_fix and re.search(rf"\b{re.escape(holding_fix)}\b", upper_txt)
            )
            if has_missed_label or has_hold_fix:
                amber_boxes.append(
                    HighlightBox(
                        color_key="AMBER",
                        x_min=ln.x_min,
                        y_min=ln.y_min,
                        x_max=ln.x_max,
                        y_max=ln.y_max,
                        label=f"Missed Apch Holding Fix: {ln.text}",
                        rule_id="AMBER_HOLDING_FIX",
                        padding=4,
                    )
                )

        # Also highlight the Holding Fix callout in the upper Profile View (e.g. ACETY above 3600, excluding icon boxes)
        if holding_fix:
            for ln in lines:
                if prof_top_y < ln.center_y < (prof_top_y + 0.065 * H) and ln.center_x > 0.25 * W:
                    for tok in ln.tokens:
                        in_icon = any(
                            bx0 - 4 <= tok.center_x <= bx1 + 4 and by0 - 4 <= tok.center_y <= by1 + 4
                            for (bx0, by0, bx1, by1) in layout.profile_icon_boxes
                        )
                        if not in_icon and re.search(rf"\b{re.escape(holding_fix)}\b", tok.text.upper()):
                            amber_boxes.append(
                                HighlightBox(
                                    color_key="AMBER",
                                    x_min=tok.x_min,
                                    y_min=tok.y_min,
                                    x_max=tok.x_max,
                                    y_max=tok.y_max,
                                    label=f"Profile Holding Fix: {tok.text}",
                                    rule_id="AMBER_HOLDING_FIX",
                                    padding=3,
                                )
                            )

        # Use exact PDF vector dashed missed-approach polylines when available
        if layout.pdf_plan_missed_chains:
            for ch in layout.pdf_plan_missed_chains:
                xs = [p[0] for p in ch]
                ys = [p[1] for p in ch]
                amber_boxes.insert(
                    0,
                    HighlightBox(
                        color_key="AMBER",
                        x_min=min(xs),
                        y_min=min(ys),
                        x_max=max(xs),
                        y_max=max(ys),
                        label="Plan View Missed Approach Escape Track",
                        rule_id="AMBER_PLAN_ESCAPE_PATH",
                        shape="polyline",
                        padding=max(8, int(round(0.0085 * W))),
                        points=ch,
                    ),
                )
        elif rwy_point is not None:
            missed_dots = layout.detect_plan_missed_track_dots(rwy_point)
            if missed_dots:
                xs = [p[0] for p in missed_dots]
                ys = [p[1] for p in missed_dots]
                amber_boxes.insert(
                    0,
                    HighlightBox(
                        color_key="AMBER",
                        x_min=min(xs),
                        y_min=min(ys),
                        x_max=max(xs),
                        y_max=max(ys),
                        label="Plan View Missed Approach Escape Track",
                        rule_id="AMBER_PLAN_ESCAPE_PATH",
                        shape="dot_path",
                        padding=8,
                        points=missed_dots,
                    ),
                )

        if layout.pdf_profile_missed_chains:
            for ch in layout.pdf_profile_missed_chains:
                xs = [p[0] for p in ch]
                ys = [p[1] for p in ch]
                amber_boxes.insert(
                    0,
                    HighlightBox(
                        color_key="AMBER",
                        x_min=min(xs),
                        y_min=min(ys),
                        x_max=max(xs),
                        y_max=max(ys),
                        label="Profile View Missed Approach Climb Path",
                        rule_id="AMBER_PROFILE_ESCAPE_PATH",
                        shape="polyline",
                        padding=max(7, int(round(0.0075 * W))),
                        points=ch,
                    ),
                )

        return amber_boxes, red_from_missed

    # -------------------------------------------------------------------------
    # D. RED Rules ("Gotchas," Restrictions & Hazards)
    # -------------------------------------------------------------------------
    def _evaluate_red_gotchas(
        self,
        tokens: List[OCRToken],
        lines: List[OCRLine],
        layout: PlateLayout,
        meta: ExtractedPlateMetadata,
        extra_red_boxes: Optional[List[HighlightBox]] = None,
        magenta_bboxes: Optional[Set[Tuple[int, int, int, int]]] = None,
        comms_line_bboxes: Optional[Set[Tuple[int, int, int, int]]] = None,
        comms_top_y: Optional[int] = None,
    ) -> List[HighlightBox]:
        W, H = layout.W, layout.H
        red_boxes: List[HighlightBox] = list(extra_red_boxes or [])
        mag_excl = magenta_bboxes or set()
        comms_excl = comms_line_bboxes or set()
        max_briefing_y = min(int(0.182 * H), comms_top_y) if comms_top_y is not None else int(0.182 * H)

        # 1. Briefing Strip Notes / Restrictions (T / A Box: left of MISSED APPROACH box)
        ma_header_toks = [
            t
            for t in tokens
            if 0.105 * H < t.center_y < max_briefing_y
            and t.center_x > 0.45 * W
            and t.text.upper().rstrip(":") in ("MISSED", "APPROACH")
        ]
        ma_left_x = min((t.x_min for t in ma_header_toks), default=int(0.58 * W)) - 6

        notes_candidates: List[Tuple[OCRLine, List[OCRToken]]] = []
        for ln in lines:
            if not (0.115 * H < ln.center_y < max_briefing_y):
                continue
            if ln.bbox in comms_excl:
                continue
            if "RNP APCH" in ln.text.upper():
                continue
            if any(kw in ln.text for kw in COMMS_FACILITY_KEYWORDS) or VHF_FREQ_RE.search(ln.text) or UHF_FREQ_RE.search(ln.text):
                continue
            # Keep tokens strictly inside the T/A Notes text column (skipping 'T', 'A', 'NA' icon badges at x_max <= 0.102*W
            # while preserving words like "Circling" or "reduction" whose x_min starts around 0.098*W,
            # and stopping before the MISSED APPROACH box at ma_left_x)
            note_toks = [
                t
                for t in ln.tokens
                if t.x_max <= ma_left_x
                and not (t.text.upper() in ("T", "A", "NA") and t.x_max <= int(0.102 * W))
            ]
            if note_toks:
                notes_candidates.append((ln, note_toks))

        has_restriction_in_box = any(
            any(kw in " ".join(t.text for t in ntoks).lower() for kw in RED_RESTRICTION_KEYWORDS)
            for _, ntoks in notes_candidates
        )
        if has_restriction_in_box:
            for ln, ntoks in notes_candidates:
                note_txt = " ".join(t.text for t in ntoks)
                meta.red_restrictions.append(note_txt)
                red_boxes.append(
                    HighlightBox(
                        color_key="RED",
                        x_min=min(t.x_min for t in ntoks),
                        y_min=min(t.y_min for t in ntoks),
                        x_max=max(t.x_max for t in ntoks),
                        y_max=max(t.y_max for t in ntoks),
                        label=f"Restriction Note: {note_txt}",
                        rule_id="RED_BRIEFING_NOTE",
                        padding=3,
                    )
                )

        # 3. Glidepath Angle (GP / TCH in Profile View just above CATEGORY header)
        is_left_sketch = (
            "CATEGORY" in layout.minimums_col_bounds
            and layout.minimums_col_bounds["CATEGORY"][0] > 0.22 * W
        )
        prof_x_min = (
            layout.minimums_col_bounds["CATEGORY"][0] - 4
            if is_left_sketch
            else int(0.04 * W)
        )
        prof_x_max = int(0.96 * W) if is_left_sketch else int(0.665 * W)
        prof_top_y = (
            min(b[1] for b in layout.profile_icon_boxes) - 3
            if layout.profile_icon_boxes
            else int(0.645 * H)
        )
        cat_y_red = min(
            (ln.y_min for ln in lines if "CATEGORY" in ln.text.upper() and ln.center_y > 0.74 * H),
            default=int(0.815 * H),
        )
        vgsi_bboxes = {
            t.bbox
            for ln in lines
            if "VGSI" in ln.text.upper() or "COINCIDENT" in ln.text.upper()
            for t in ln.tokens
        }
        gp_anchor_toks = [
            t
            for t in tokens
            if (prof_top_y + 0.02 * H) < t.center_y < (cat_y_red - 2)
            and prof_x_min <= t.center_x <= prof_x_max
            and not t.is_rotated
            and t.bbox not in vgsi_bboxes
            and any(k in t.text.upper() for k in ("GP", "TCH"))
        ]
        if gp_anchor_toks:
            ref_tok = max(gp_anchor_toks, key=lambda t: t.center_y)
            # Only include non-rotated tokens that actually belong to the GP / angle / TCH callout
            # (never sweeping up nearby "3.7 NM to RW31R" or rotated "312°" course labels!)
            cluster_toks = [
                t
                for t in tokens
                if not t.is_rotated
                and abs(t.center_x - ref_tok.center_x) <= 0.036 * W
                and -0.024 * H <= (t.center_y - ref_tok.center_y) <= 0.015 * H
                and (prof_top_y + 0.02 * H) < t.center_y < (cat_y_red - 2)
                and prof_x_min <= t.center_x <= prof_x_max
                and t.bbox not in vgsi_bboxes
                and (
                    any(k in t.text.upper() for k in ("GP", "TCH"))
                    or bool(GP_ANGLE_RE.search(t.text))
                    or bool(re.match(r"^\d{2,3}°?$", t.text.strip()))
                )
            ]
            gx1 = min(t.x_min for t in cluster_toks)
            gy1 = min(t.y_min for t in cluster_toks)
            gx2 = max(t.x_max for t in cluster_toks)
            gy2 = max(t.y_max for t in cluster_toks)

            combined_gp_text = " ".join(
                t.text for t in sorted(cluster_toks, key=lambda tk: (tk.y_min, tk.x_min))
            )
            # If the plate draws a vector angle symbol '∠4.00°' instead of 'GP 4.00°', pad gx1 slightly left to cover '∠'
            if "GP" not in combined_gp_text.upper():
                gx1 = max(0, gx1 - int(0.013 * W))

            parsed_angle: Optional[float] = None
            m_gp = GP_ANGLE_RE.search(combined_gp_text)
            if m_gp:
                try:
                    parsed_angle = float(m_gp.group(1))
                    meta.glidepath_angle = parsed_angle
                except ValueError:
                    pass

            m_tch = re.search(r"TCH\s*(\d{2,3})", combined_gp_text, re.IGNORECASE)
            if m_tch:
                meta.tch = m_tch.group(1)

            border_w = 2 if (parsed_angle is not None and parsed_angle > 3.00) else 1
            snapped_gp = layout.snap_box_to_cell((gx1, gy1, gx2, gy2), max_expansion_ratio=0.012)
            if (snapped_gp[2] - snapped_gp[0]) > (gx2 - gx1) + 16 or (snapped_gp[3] - snapped_gp[1]) > (gy2 - gy1) + 16:
                snapped_gp = (gx1, gy1, gx2, gy2)

            red_boxes.append(
                HighlightBox(
                    color_key="RED",
                    x_min=snapped_gp[0],
                    y_min=snapped_gp[1],
                    x_max=snapped_gp[2],
                    y_max=snapped_gp[3],
                    label=f"Glidepath/TCH: {combined_gp_text}"
                    + (f" (STEEP {parsed_angle:.2f}° > 3.00°)" if border_w == 2 else ""),
                    rule_id="RED_GLIDEPATH_TCH",
                    border_width=border_w,
                    padding=3,
                )
            )

        # 4. Plan View Arrival Restrictions, Course Offset Notes, MSA Circle Ring & High Terrain
        plan_lines = [
            ln
            for ln in lines
            if 0.18 * H < ln.center_y < prof_top_y
            and not (is_left_sketch and ln.center_x < prof_x_min and ln.center_y > 0.595 * H)
        ]
        msa_anchor_lines: List[OCRLine] = []
        flagged_na_bboxes: Set[Tuple[int, int, int, int]] = set()
        for ln in plan_lines:
            low = ln.text.lower()
            upper = ln.text.upper()

            if (
                "procedure na for arrival" in low
                or "procedure na" in low
                or "final approach course" in low
                or "course offset" in low
            ):
                # Also include any immediate continuation line directly underneath this note
                note_group = [ln]
                for other in plan_lines:
                    if (
                        other is not ln
                        and not other.is_rotated
                        and 0 < other.center_y - ln.center_y <= 0.024 * H
                        and abs(other.x_min - ln.x_min) <= 0.025 * W
                    ):
                        note_group.append(other)
                for n_ln in note_group:
                    if n_ln.bbox not in flagged_na_bboxes:
                        flagged_na_bboxes.add(n_ln.bbox)
                        meta.red_restrictions.append(n_ln.text)
                        red_boxes.append(
                            HighlightBox(
                                color_key="RED",
                                x_min=n_ln.x_min,
                                y_min=n_ln.y_min,
                                x_max=n_ln.x_max,
                                y_max=n_ln.y_max,
                                label=f"Arrival / Course Restriction: {n_ln.text}",
                                rule_id="RED_ARRIVAL_NA",
                                padding=3,
                            )
                        )

            if MSA_HEADER_RE.search(upper):
                msa_anchor_lines.append(ln)

        # Find the 4-digit MSA altitude token inside the PDF vector MSA circle or near MSA header
        msa_alt_tok: Optional[OCRToken] = None
        if layout.pdf_msa_circle is not None:
            mc = layout.pdf_msa_circle
            for tok in tokens:
                m_alt = SPOT_ELEV_RE.search(tok.text)
                if m_alt and tok.text.endswith("00"):
                    dist = ((tok.center_x - mc.center_x) ** 2 + (tok.center_y - mc.center_y) ** 2) ** 0.5
                    if dist <= mc.radius * 0.95:
                        msa_alt_tok = tok
                        break

        if msa_alt_tok is None:
            for msa_ln in msa_anchor_lines:
                for tok in tokens:
                    if not (0.22 * H < tok.center_y < 0.62 * H):
                        continue
                    m_alt = SPOT_ELEV_RE.search(tok.text)
                    if m_alt and tok.text.endswith("00"):
                        dist = ((tok.center_x - msa_ln.center_x) ** 2 + (tok.center_y - msa_ln.center_y) ** 2) ** 0.5
                        if dist <= 0.12 * W:
                            msa_alt_tok = tok
                            break
                if msa_alt_tok is not None:
                    break

        if msa_alt_tok is not None:
            alt_val = SPOT_ELEV_RE.search(msa_alt_tok.text).group(1)  # type: ignore[union-attr]
            if alt_val not in meta.msa_altitudes:
                meta.msa_altitudes.append(alt_val)

            fitted_circle = layout.fit_msa_circle(
                int(msa_alt_tok.center_x), int(msa_alt_tok.center_y)
            )
            cx, cy, r = fitted_circle.center_x, fitted_circle.center_y, fitted_circle.radius

            red_boxes.append(
                HighlightBox(
                    color_key="RED",
                    x_min=max(0, cx - r),
                    y_min=max(0, cy - r),
                    x_max=min(W, cx + r),
                    y_max=min(H, cy + r),
                    label=f"MSA 25 NM Circle ({alt_val} ft)",
                    rule_id="RED_MSA_CIRCLE",
                    shape="circle",
                    border_width=2,
                    padding=2,
                )
            )
            inner_bbox = (
                layout.pdf_msa_inner_box
                if layout.pdf_msa_inner_box is not None
                else (msa_alt_tok.x_min, msa_alt_tok.y_min, msa_alt_tok.x_max, msa_alt_tok.y_max)
            )
            red_boxes.append(
                HighlightBox(
                    color_key="RED",
                    x_min=inner_bbox[0],
                    y_min=inner_bbox[1],
                    x_max=inner_bbox[2],
                    y_max=inner_bbox[3],
                    label=f"MSA Altitude: {alt_val}",
                    rule_id="RED_MSA_ALTITUDE",
                    border_width=2,
                    padding=2,
                )
            )
        elif layout.circles:
            c = layout.circles[0]
            red_boxes.append(
                HighlightBox(
                    color_key="RED",
                    x_min=max(0, c.center_x - c.radius),
                    y_min=max(0, c.center_y - c.radius),
                    x_max=min(W, c.center_x + c.radius),
                    y_max=min(H, c.center_y + c.radius),
                    label="MSA Circle",
                    rule_id="RED_MSA_CIRCLE",
                    shape="circle",
                    border_width=2,
                    padding=2,
                )
            )

        # Highlight 4-digit spot elevations/obstacles (\b[2-9]\d{3}\b) inside Plan View in RED
        # Exclude contour-line labels (multiples of 1000 like 1000, 2000, 3000) and
        # feeder airway MEA altitudes (e.g. 3600, 5600) so only genuine spot obstacles are flagged
        existing_boxes = [(b.x_min, b.y_min, b.x_max, b.y_max) for b in red_boxes]
        for ln in plan_lines:
            if not (0.20 * H < ln.center_y < 0.60 * H):
                continue
            if ln.bbox in mag_excl:
                continue
            upper_txt = ln.text.upper()
            if any(k in upper_txt for k in ("(FAF)", "(IF)", "(IAF)", "MSA", "OCT", "PROCEDURE")):
                continue
            m_elev = SPOT_ELEV_RE.search(upper_txt)
            if m_elev:
                elev_val = m_elev.group(1)
                # Skip terrain contour labels (1000, 2000, 3000, 4000) and airway MEAs ending in 00
                if elev_val.endswith("00"):
                    continue
                already_covered = any(
                    abs(ln.center_x - (bx1 + bx2) / 2.0) < 18
                    and abs(ln.center_y - (by1 + by2) / 2.0) < 18
                    for bx1, by1, bx2, by2 in existing_boxes
                )
                if not already_covered:
                    if elev_val not in meta.high_obstacles:
                        meta.high_obstacles.append(elev_val)
                    red_boxes.append(
                        HighlightBox(
                            color_key="RED",
                            x_min=ln.x_min,
                            y_min=ln.y_min,
                            x_max=ln.x_max,
                            y_max=ln.y_max,
                            label=f"Spot Elevation / Obstacle: {elev_val} ft",
                            rule_id="RED_SPOT_ELEVATION",
                            padding=3,
                        )
                    )

        return red_boxes
