"""
Native FAA d-TPP Vector PDF Parser & Path Geometry Engine (`enhancer/pdf_engine.py`).

Extracts 100% deterministic vector text spans, rotated text quadrilaterals,
table grid cells, bold approach course corridors (`lw ≈ 1.44`), profile descent
slope vectors (`lw ≈ 1.44`), curved dashed missed-approach escape polylines
(`lw ≈ 0.72`), holding pattern racetracks, and the exact 16-Bezier MSA circle
directly from official FAA d-TPP PDFs using PyMuPDF (`pymupdf`).

Safety-Critical Requirement:
The PDF page is rendered once at high resolution (`dpi_scale = 3.0`, ~216 DPI)
into an immutable `RGBA` base image (`locked_base`). All highlights are drawn
on a separate transparent RGBA overlay and composited via `Image.alpha_composite`.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import cv2
import numpy as np
import pymupdf
from PIL import Image

from enhancer.cv_layout import DetectedCircle, PlateLayout, _cluster_1d, _dedup_boxes
from enhancer.ocr_engine import OCRLine, OCRToken

FRACTION_MAP: Dict[str, str] = {
    "14": "1/4",
    "12": "1/2",
    "34": "3/4",
    "18": "1/8",
    "38": "3/8",
    "58": "5/8",
    "78": "7/8",
}


def load_pdf_plate(
    pdf_source: Union[str, Path, bytes],
    page_index: int = 0,
    dpi_scale: float = 3.0,
) -> Tuple[Image.Image, List[OCRToken], List[OCRLine], PlateLayout]:
    """
    Open an FAA d-TPP PDF from a file path or raw bytes, render the untouched
    `RGBA` base image at `dpi_scale`, and extract exact vector text (`tokens`,
    `lines`) and vector path/table layout (`PlateLayout`).
    """
    if isinstance(pdf_source, (bytes, bytearray)):
        doc = pymupdf.open(stream=bytes(pdf_source), filetype="pdf")
    else:
        doc = pymupdf.open(str(pdf_source))

    try:
        page = doc[page_index]
        mat = pymupdf.Matrix(dpi_scale, dpi_scale)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        locked_base = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("RGBA")

        W, H = locked_base.size
        sx = W / max(1.0, float(page.rect.width))
        sy = H / max(1.0, float(page.rect.height))

        layout = _extract_pdf_vector_layout(page, locked_base, sx, sy)
        tokens, lines = _extract_pdf_vector_text(page, W, H, sx, sy, layout.vertical_lines_x)
        _populate_minimums_col_bounds(page, tokens, layout, sx, sy)
        return locked_base, tokens, lines, layout
    finally:
        doc.close()


def _populate_minimums_col_bounds(
    page: pymupdf.Page,
    tokens: List[OCRToken],
    layout: PlateLayout,
    sx: float,
    sy: float,
) -> None:
    """
    Locate the `CATEGORY | A | B | C | D` row in `y > 0.74 * H` and record the
    exact column boundaries `[x_left, x_right]` for `CATEGORY`, `A`, `B`, `C`, `D`
    in `layout.minimums_col_bounds` using only vertical lines that cross the
    Minimums Table (`y_bot >= cat_tok.y_min`).
    """
    cat_toks = [
        t for t in tokens if "CATEGORY" in t.text.upper() and t.center_y > 0.74 * layout.H
    ]
    if not cat_toks:
        return
    cat_tok = min(cat_toks, key=lambda t: t.center_y)

    min_table_v_lines: List[int] = []
    for d in page.get_drawings():
        items = d.get("items", [])
        r = d.get("rect")
        if r is None or not items:
            continue
        if len(items) == 1 and items[0][0] == "l":
            _, p1, p2 = items[0]
            if abs(p1.x - p2.x) <= 0.8:
                yt = min(p1.y, p2.y) * sy
                yb = max(p1.y, p2.y) * sy
                if yb >= cat_tok.y_min + 2 and yt <= cat_tok.y_max + 20:
                    min_table_v_lines.append(int(round(((p1.x + p2.x) / 2.0) * sx)))
        elif len(items) == 1 and items[0][0] in ("qu", "re"):
            yt = r.y0 * sy
            yb = r.y1 * sy
            if yb >= cat_tok.y_min + 2 and yt <= cat_tok.y_max + 20 and r.height >= 10.0:
                min_table_v_lines.extend([int(round(r.x0 * sx)), int(round(r.x1 * sx))])

    v_lines = sorted(set(min_table_v_lines)) if min_table_v_lines else sorted(layout.vertical_lines_x)
    left_candidates = [vx for vx in v_lines if vx <= cat_tok.x_min + 6]
    if left_candidates:
        table_left = max(left_candidates)
        right_candidates = [vx for vx in v_lines if vx >= cat_tok.x_max - 4]
        table_cat_right = min(right_candidates) if right_candidates else cat_tok.x_max + 10
        layout.minimums_col_bounds["CATEGORY"] = (table_left, table_cat_right)

    for letter in ("A", "B", "C", "D"):
        ltoks = [
            t
            for t in tokens
            if t.text.strip().upper() == letter
            and abs(t.center_y - cat_tok.center_y) <= 0.018 * layout.H
            and t.center_x > cat_tok.center_x
        ]
        if ltoks:
            lt = ltoks[0]
            l_v = [vx for vx in v_lines if vx <= lt.x_min - 8]
            r_v = [vx for vx in v_lines if vx >= lt.x_max + 8]
            col_l = max(l_v) if l_v else int(lt.center_x - 0.055 * layout.W)
            col_r = min(r_v) if r_v else int(lt.center_x + 0.055 * layout.W)
            layout.minimums_col_bounds[letter] = (col_l, col_r)


def _extract_pdf_vector_text(
    page: pymupdf.Page,
    W: int,
    H: int,
    sx: float,
    sy: float,
    vertical_lines_x: List[int],
) -> Tuple[List[OCRToken], List[OCRLine]]:
    """
    Extract exact vector text lines and tokens from `page.get_text("dict")`,
    preserving rotated `quad_points` and `dir_vec` for angled course/altitude
    labels and merging horizontal lines within the same cell.
    """
    text_dict = page.get_text("dict")
    raw_lines: List[OCRLine] = []
    all_tokens: List[OCRToken] = []

    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for ln in block.get("lines", []):
            spans = ln.get("spans", [])
            if not spans:
                continue
            full_txt = "".join(s.get("text", "") for s in spans).strip()
            full_txt = re.sub(r"\s+", " ", full_txt)
            if not full_txt:
                continue

            dx, dy = ln.get("dir", (1.0, 0.0))
            dir_vec = (float(dx), float(dy))
            bbox_pdf = ln.get("bbox", (0.0, 0.0, 0.0, 0.0))
            x0 = max(0, int(round(bbox_pdf[0] * sx)))
            y0 = max(0, int(round(bbox_pdf[1] * sy)))
            x1 = min(W, int(round(bbox_pdf[2] * sx)))
            y1 = min(H, int(round(bbox_pdf[3] * sy)))

            if x1 <= x0 or y1 <= y0:
                continue

            cx = (x0 + x1) / 2.0
            cy = (y0 + y1) / 2.0

            # Exclude vertical side-margin volume/date labels
            if (cx < 0.046 * W or cx > 0.954 * W) and (abs(dy) > 0.6 or (y1 - y0) > (x1 - x0) * 2.2):
                continue

            # Exclude single-letter 'T' and 'A' inverted black triangle badges
            if full_txt in ("T", "A") and cx < 0.085 * W and 0.11 * H < cy < 0.17 * H:
                continue

            # Recover exact 4-corner quad for rotated text using baseline origin, dir_vec, and font size
            is_rot = abs(dy) > 0.12 or dx < 0.95
            quad_pts: Optional[List[Tuple[int, int]]] = None
            if is_rot and "origin" in spans[0]:
                ox, oy = float(spans[0]["origin"][0]), float(spans[0]["origin"][1])
                fsize = max(float(s.get("size", 8.0)) for s in spans)
                # Upward normal to baseline (in PDF top-down coordinates)
                nx, ny = dy, -dx
                h_top = 0.82 * fsize
                h_bot = -0.16 * fsize
                bw = float(bbox_pdf[2] - bbox_pdf[0])
                bh = float(bbox_pdf[3] - bbox_pdf[1])
                if abs(dx) >= abs(dy):
                    adv_len = (bw - (h_top - h_bot) * abs(dy)) / max(0.2, abs(dx))
                else:
                    adv_len = (bh - (h_top - h_bot) * abs(dx)) / max(0.2, abs(dy))
                if len(spans) > 1 and "origin" in spans[-1]:
                    ox_last, oy_last = float(spans[-1]["origin"][0]), float(spans[-1]["origin"][1])
                    d_orig = math.hypot(ox_last - ox, oy_last - oy)
                    adv_len = max(adv_len, d_orig + 0.45 * fsize)
                adv_len = max(adv_len, 0.45 * fsize * max(1, len(full_txt)))
                t_start = -0.05 * fsize
                t_end = adv_len
                ul_x = ox + t_start * dx + h_top * nx
                ul_y = oy + t_start * dy + h_top * ny
                ur_x = ox + t_end * dx + h_top * nx
                ur_y = oy + t_end * dy + h_top * ny
                lr_x = ox + t_end * dx + h_bot * nx
                lr_y = oy + t_end * dy + h_bot * ny
                ll_x = ox + t_start * dx + h_bot * nx
                ll_y = oy + t_start * dy + h_bot * ny
                quad_pts = [
                    (int(round(ul_x * sx)), int(round(ul_y * sy))),
                    (int(round(ur_x * sx)), int(round(ur_y * sy))),
                    (int(round(lr_x * sx)), int(round(lr_y * sy))),
                    (int(round(ll_x * sx)), int(round(ll_y * sy))),
                ]
                qxs = [p[0] for p in quad_pts]
                qys = [p[1] for p in quad_pts]
                x0, y0, x1, y1 = max(0, min(qxs)), max(0, min(qys)), min(W, max(qxs)), min(H, max(qys))
            else:
                quad_pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            if is_rot:
                tok = OCRToken(
                    text=full_txt,
                    x_min=x0,
                    y_min=y0,
                    x_max=x1,
                    y_max=y1,
                    confidence=1.0,
                    W=W,
                    H=H,
                    dir_vec=dir_vec,
                    quad_points=quad_pts,
                )
                all_tokens.append(tok)
                raw_lines.append(
                    OCRLine(
                        tokens=[tok],
                        W=W,
                        H=H,
                        dir_vec=dir_vec,
                        quad_points=quad_pts,
                    )
                )
            else:
                line_toks: List[OCRToken] = []
                for s in spans:
                    s_raw = s.get("text", "")
                    if not s_raw or not s_raw.strip():
                        continue
                    sb = s.get("bbox", bbox_pdf)
                    sx0 = max(0, int(round(sb[0] * sx)))
                    sy0 = max(0, int(round(sb[1] * sy)))
                    sx1 = min(W, int(round(sb[2] * sx)))
                    sy1 = min(H, int(round(sb[3] * sy)))
                    s_len = max(1, len(s_raw))
                    s_w = float(max(1, sx1 - sx0))
                    for m_w in re.finditer(r"\S+", s_raw):
                        w_str = m_w.group(0)
                        tx0 = int(round(sx0 + s_w * (m_w.start() / s_len)))
                        tx1 = min(sx1, int(round(sx0 + s_w * (m_w.end() / s_len))))
                        tok = OCRToken(
                            text=w_str,
                            x_min=tx0,
                            y_min=sy0,
                            x_max=max(tx0 + 1, tx1),
                            y_max=max(sy0 + 1, sy1),
                            confidence=1.0,
                            W=W,
                            H=H,
                            dir_vec=dir_vec,
                            quad_points=None,
                        )
                        line_toks.append(tok)
                        all_tokens.append(tok)
                if line_toks:
                    line_toks.sort(key=lambda t: t.x_min)
                    raw_lines.append(
                        OCRLine(
                            tokens=line_toks,
                            W=W,
                            H=H,
                            dir_vec=dir_vec,
                            quad_points=quad_pts,
                        )
                    )

    # Merge horizontal lines on the same visual row inside the same cell
    # (e.g., merging '*' with 'Missed approach requires...', '*' with 'LPV DA',
    # or fraction glyphs like '34' -> '3/4' inside 'reduction below SM NA.')
    merged_lines = _merge_horizontal_pdf_lines(raw_lines, W, H, sx, sy, vertical_lines_x)
    return all_tokens, merged_lines


def _merge_horizontal_pdf_lines(
    raw_lines: List[OCRLine],
    W: int,
    H: int,
    sx: float,
    sy: float,
    vertical_lines_x: List[int],
) -> List[OCRLine]:
    rotated = [ln for ln in raw_lines if ln.is_rotated]
    horiz = [ln for ln in raw_lines if not ln.is_rotated]
    horiz.sort(key=lambda l: (round(l.center_y / max(1.0, 3.0 * sy)), l.x_min))

    merged: List[OCRLine] = []
    for ln in horiz:
        txt = ln.text.strip()
        # Convert standalone 2-digit fraction blocks (like '34' inside 'below   SM' or '78' inside minimums)
        if txt in FRACTION_MAP and ln.width <= 12 * sx:
            for t in ln.tokens:
                if t.text in FRACTION_MAP:
                    t.text = FRACTION_MAP[t.text]

        placed = False
        for m in merged:
            if abs(ln.center_y - m.center_y) <= 3.5 * sy:
                # Check if ln is inside m horizontally or very close without crossing a vertical divider
                x_left = min(ln.x_max, m.x_max)
                x_right = max(ln.x_min, m.x_min)
                gap = x_right - x_left
                crosses_divider = any(
                    x_left - 2 <= vx <= x_right + 2 for vx in vertical_lines_x
                )
                if gap <= 12.0 * sx and not crosses_divider:
                    m.tokens.extend(ln.tokens)
                    m.tokens.sort(key=lambda t: t.x_min)
                    placed = True
                    break
        if not placed:
            merged.append(ln)

    all_out = merged + rotated
    all_out.sort(key=lambda l: (l.y_min, l.x_min))
    return all_out


def _extract_pdf_vector_layout(
    page: pymupdf.Page,
    locked_base: Image.Image,
    sx: float,
    sy: float,
) -> PlateLayout:
    """
    Extract exact vector table grid lines, cells, 3 profile missed-approach icon boxes,
    16-Bezier MSA circle, Plan View bold course corridors (`lw ≈ 1.44`), Profile View
    descent slope segment (`lw ≈ 1.44`), and curved dashed missed-approach polylines
    (`lw ≈ 0.72`) directly from `page.get_drawings()`.
    """
    W, H = locked_base.size
    W_pdf = float(page.rect.width)
    H_pdf = float(page.rect.height)
    rgb = np.array(locked_base.convert("RGB"), copy=True)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    drawings = page.get_drawings()

    raw_h_lines: List[int] = []
    raw_v_lines: List[int] = []
    quad_boxes: List[Tuple[int, int, int, int]] = []
    plan_144_segs: List[Tuple[float, float, float, float, float, float]] = []
    prof_144_segs: List[Tuple[float, float, float, float, float, float]] = []
    arrowheads: List[Tuple[float, float, float, float]] = []
    raw_072_dashes: List[Tuple[int, float, float, float, float]] = []
    msa_circle: Optional[DetectedCircle] = None
    msa_inner_box: Optional[Tuple[int, int, int, int]] = None

    for idx, d in enumerate(drawings):
        r = d.get("rect")
        if r is None:
            continue
        lw = d.get("width")
        color = d.get("color")
        fill = d.get("fill")
        items = d.get("items", [])
        if not items:
            continue

        # 1. Detect exact 16-Bezier MSA 25 NM Circle in Plan View
        if (
            len(items) >= 8
            and all(it[0] == "c" for it in items)
            and 42.0 <= r.width <= 85.0
            and abs(r.width - r.height) <= 3.5
            and 0.18 * H_pdf <= (r.y0 + r.y1) / 2.0 <= 0.68 * H_pdf
        ):
            cx_px = int(round(((r.x0 + r.x1) / 2.0) * sx))
            cy_px = int(round(((r.y0 + r.y1) / 2.0) * sy))
            rad_px = int(round((r.width / 2.0) * ((sx + sy) / 2.0)))
            msa_circle = DetectedCircle(center_x=cx_px, center_y=cy_px, radius=rad_px)
            continue

        # 2. Detect filled black arrowheads (4..9 line segments, small bounding box)
        if (
            fill == (0.0, 0.0, 0.0)
            and 3 <= len(items) <= 10
            and all(it[0] == "l" for it in items)
            and 2.0 <= r.width <= 15.0
            and 2.0 <= r.height <= 15.0
        ):
            arrowheads.append((r.x0, r.y0, r.x1, r.y1))

        # 3. Quad boxes ('qu') or rectangles ('re')
        if len(items) == 1 and items[0][0] in ("qu", "re"):
            bx0 = int(round(r.x0 * sx))
            by0 = int(round(r.y0 * sy))
            bx1 = int(round(r.x1 * sx))
            by1 = int(round(r.y1 * sy))
            if (bx1 - bx0) >= int(10 * sx) and (by1 - by0) >= int(5 * sy):
                quad_boxes.append((bx0, by0, bx1, by1))
            if r.width >= 20.0:
                raw_h_lines.extend([by0, by1])
            if r.height >= 12.0:
                raw_v_lines.extend([bx0, bx1])

        # 4. Single line segments ('l')
        if len(items) == 1 and items[0][0] == "l":
            _, p1, p2 = items[0]
            dx = float(p2.x - p1.x)
            dy = float(p2.y - p1.y)
            seg_len = math.hypot(dx, dy)
            cy_pdf = (p1.y + p2.y) / 2.0
            cx_pdf = (p1.x + p2.x) / 2.0

            # Horizontal grid line
            if abs(dy) <= 0.8 and abs(dx) >= 14.0:
                raw_h_lines.append(int(round(cy_pdf * sy)))

            # Vertical grid line
            if abs(dx) <= 0.8 and abs(dy) >= 10.0:
                raw_v_lines.append(int(round(cx_pdf * sx)))

            # Bold course / descent slope line segment (lw ≈ 1.44)
            if lw is not None and 1.15 <= lw <= 1.78 and seg_len >= 2.0:
                ang = math.degrees(math.atan2(dy, dx)) % 180.0
                if 0.20 * H_pdf <= cy_pdf <= 0.625 * H_pdf:
                    plan_144_segs.append((float(p1.x), float(p1.y), float(p2.x), float(p2.y), ang, seg_len))
                elif 0.635 * H_pdf <= cy_pdf <= 0.835 * H_pdf and cx_pdf >= 0.16 * W_pdf:
                    # Exclude purely vertical or horizontal lines in profile view
                    if abs(dx) >= 4.0 and 0.10 <= abs(dy / dx) <= 0.75:
                        prof_144_segs.append((float(p1.x), float(p1.y), float(p2.x), float(p2.y), ang, seg_len))

            # Dashed missed-approach / holding track segment (lw ≈ 0.72)
            elif lw is not None and 0.55 <= lw <= 0.88 and 0.3 <= seg_len <= 5.0:
                raw_072_dashes.append(
                    (idx, (float(p1.x + p2.x) / 2.0), (float(p1.y + p2.y) / 2.0), float(p1.x), float(p1.y))
                )

    horizontal_lines_y = _cluster_1d(raw_h_lines, min_gap=max(3, int(2.5 * sy)))
    vertical_lines_x = _cluster_1d(raw_v_lines, min_gap=max(3, int(2.5 * sx)))

    # Locate inner MSA 4-digit altitude box if msa_circle was found
    if msa_circle is not None:
        for bx0, by0, bx1, by1 in quad_boxes:
            bcx = (bx0 + bx1) / 2.0
            bcy = (by0 + by1) / 2.0
            if (
                math.hypot(bcx - msa_circle.center_x, bcy - msa_circle.center_y) <= msa_circle.radius * 0.85
                and (bx1 - bx0) <= msa_circle.radius * 1.1
                and (by1 - by0) <= msa_circle.radius * 0.55
            ):
                msa_inner_box = (bx0, by0, bx1, by1)
                break

    # Reconstruct exact 3 top-left Profile View Missed Approach icon boxes from PDF vector grid lines
    profile_icon_boxes = _extract_profile_icon_boxes(page, sx, sy, W_pdf, H_pdf)

    # Build table cells via OpenCV on the grid lines + quad_boxes for cell snapping
    table_cells = list(quad_boxes)
    table_cells.extend(profile_icon_boxes)
    table_cells = _dedup_boxes(table_cells)

    # Group colinear Plan View bold course segments (`lw ≈ 1.44`)
    pdf_plan_course_groups = _group_colinear_course_segments(plan_144_segs, arrowheads, sx, sy)

    # Group colinear Profile View descent slope segments (`lw ≈ 1.44`)
    pdf_profile_slope_seg = _group_profile_slope_segments(prof_144_segs, arrowheads, sx, sy)

    # Chain `lw ≈ 0.72` dashes into Plan View missed approach curves, Holding racetracks,
    # and Profile View missed approach climb curves
    pdf_plan_missed_chains, pdf_holding_chains, pdf_profile_missed_chains = _chain_dashed_tracks(
        raw_072_dashes, arrowheads, W_pdf, H_pdf, sx, sy
    )

    circles_list = [msa_circle] if msa_circle is not None else []

    return PlateLayout(
        W=W,
        H=H,
        gray=gray,
        horizontal_lines_y=horizontal_lines_y,
        vertical_lines_x=vertical_lines_x,
        table_cells=table_cells,
        profile_icon_boxes=profile_icon_boxes[:3],
        circles=circles_list,
        is_pdf_vector=True,
        pdf_plan_course_groups=pdf_plan_course_groups,
        pdf_profile_slope_seg=pdf_profile_slope_seg,
        pdf_plan_missed_chains=pdf_plan_missed_chains,
        pdf_profile_missed_chains=pdf_profile_missed_chains,
        pdf_holding_chains=pdf_holding_chains,
        pdf_msa_circle=msa_circle,
        pdf_msa_inner_box=msa_inner_box,
    )


def _extract_profile_icon_boxes(
    page: pymupdf.Page,
    sx: float,
    sy: float,
    W_pdf: float,
    H_pdf: float,
) -> List[Tuple[int, int, int, int]]:
    """
    Find the 1..3 procedural Missed Approach icon boxes at the top-left of the
    Profile View (`0.61 * H_pdf <= y <= 0.73 * H_pdf`, `x <= 0.42 * W_pdf`)
    directly from the PDF's horizontal and vertical vector lines.
    """
    drawings = page.get_drawings()
    v_segs: List[Tuple[float, float, float]] = []
    h_segs: List[Tuple[float, float, float]] = []

    for d in drawings:
        items = d.get("items", [])
        if len(items) == 1 and items[0][0] == "l":
            _, p1, p2 = items[0]
            if abs(p1.x - p2.x) <= 0.8:
                y_top, y_bot = min(p1.y, p2.y), max(p1.y, p2.y)
                h = y_bot - y_top
                if (
                    0.61 * H_pdf <= y_top <= 0.72 * H_pdf
                    and 28.0 <= h <= 38.0
                    and p1.x <= 0.52 * W_pdf
                ):
                    v_segs.append((float(p1.x), float(y_top), float(y_bot)))
            elif abs(p1.y - p2.y) <= 0.8:
                x_left, x_right = min(p1.x, p2.x), max(p1.x, p2.x)
                if 0.61 * H_pdf <= p1.y <= 0.77 * H_pdf and x_left <= 0.45 * W_pdf and (x_right - x_left) >= 15.0:
                    h_segs.append((float(p1.y), float(x_left), float(x_right)))

    if not v_segs:
        return []

    # Cluster vertical dividers at the same (y_top, y_bot) band
    y_top_med = float(np.median([v[1] for v in v_segs]))
    y_bot_med = float(np.median([v[2] for v in v_segs]))
    xs = sorted(set(round(v[0], 1) for v in v_segs if abs(v[1] - y_top_med) <= 4.0 and abs(v[2] - y_bot_med) <= 4.0))

    # Find the left-most border of the first icon box from horizontal bottom/top borders or outer frame
    matching_h_lefts = [
        hs[1]
        for hs in h_segs
        if (abs(hs[0] - y_bot_med) <= 2.5 or abs(hs[0] - y_top_med) <= 2.5)
        and hs[1] < (xs[0] - 12.0 if xs else 200.0)
    ]
    if matching_h_lefts:
        left_border_x = min(matching_h_lefts)
        xs.insert(0, round(left_border_x, 1))
    elif xs and xs[0] <= 50.0 and xs[0] > 25.0:
        xs.insert(0, 18.2)

    # Deduplicate xs within 3 pt
    clean_xs: List[float] = []
    for x in xs:
        if not clean_xs or x - clean_xs[-1] >= 12.0:
            clean_xs.append(x)

    boxes: List[Tuple[int, int, int, int]] = []
    for i in range(len(clean_xs) - 1):
        w_pt = clean_xs[i + 1] - clean_xs[i]
        if 15.0 <= w_pt <= 42.0:
            boxes.append(
                (
                    int(round(clean_xs[i] * sx)),
                    int(round(y_top_med * sy)),
                    int(round(clean_xs[i + 1] * sx)),
                    int(round(y_bot_med * sy)),
                )
            )
    return boxes[:3]


def _group_colinear_course_segments(
    segs: List[Tuple[float, float, float, float, float, float]],
    arrowheads: List[Tuple[float, float, float, float]],
    sx: float,
    sy: float,
) -> List[Tuple[int, int, int, int]]:
    """
    Merge colinear `lw ≈ 1.44` segments in the Plan View into continuous straight
    flight-path vectors `(x1, y1, x2, y2)` in pixel space, extending to any
    touching arrowhead at the end of the segment.
    """
    groups: List[List[Tuple[float, float, float, float, float, float]]] = []
    for s in segs:
        x1, y1, x2, y2, ang, length = s
        placed = False
        for g in groups:
            gx1, gy1, gx2, gy2, gang, _ = g[0]
            adiff = min(abs(ang - gang), 180.0 - abs(ang - gang))
            if adiff <= 6.0:
                gdx, gdy = gx2 - gx1, gy2 - gy1
                glen = max(1e-6, math.hypot(gdx, gdy))
                pdist = abs((x1 - gx1) * gdy - (y1 - gy1) * gdx) / glen
                if pdist <= 4.0:
                    g.append(s)
                    placed = True
                    break
        if not placed:
            groups.append([s])

    out_vectors: List[Tuple[int, int, int, int]] = []
    for g in groups:
        pts: List[Tuple[float, float]] = []
        for s in g:
            pts.append((s[0], s[1]))
            pts.append((s[2], s[3]))

        # Check if any filled arrowhead sits within 6 pt of any endpoint along the same line
        gx1, gy1, gx2, gy2, _, _ = g[0]
        gdx, gdy = gx2 - gx1, gy2 - gy1
        glen = max(1e-6, math.hypot(gdx, gdy))
        for ax0, ay0, ax1, ay1 in arrowheads:
            acx, acy = (ax0 + ax1) / 2.0, (ay0 + ay1) / 2.0
            pdist = abs((acx - gx1) * gdy - (acy - gy1) * gdx) / glen
            if pdist <= 4.0:
                if min(math.hypot(acx - px, acy - py) for px, py in pts) <= 8.0:
                    pts.append((acx, acy))

        max_d = -1.0
        best_pair: Optional[Tuple[Tuple[float, float], Tuple[float, float]]] = None
        for p_a in pts:
            for p_b in pts:
                d = math.hypot(p_b[0] - p_a[0], p_b[1] - p_a[1])
                if d > max_d:
                    max_d = d
                    best_pair = (p_a, p_b)

        if best_pair is not None and max_d >= 18.0:
            p_start, p_end = best_pair
            out_vectors.append(
                (
                    int(round(p_start[0] * sx)),
                    int(round(p_start[1] * sy)),
                    int(round(p_end[0] * sx)),
                    int(round(p_end[1] * sy)),
                )
            )

    return out_vectors


def _group_profile_slope_segments(
    segs: List[Tuple[float, float, float, float, float, float]],
    arrowheads: List[Tuple[float, float, float, float]],
    sx: float,
    sy: float,
) -> Optional[Tuple[int, int, int, int]]:
    """
    Merge colinear sloped `lw ≈ 1.44` segments in the Profile View into a single
    continuous descent slope vector `(x1, y1, x2, y2)` in pixel space.
    """
    if not segs:
        return None
    vecs = _group_colinear_course_segments(segs, arrowheads, sx, sy)
    if not vecs:
        return None
    return max(vecs, key=lambda v: math.hypot(v[2] - v[0], v[3] - v[1]))


def _chain_dashed_tracks(
    raw_072_dashes: List[Tuple[int, float, float, float, float]],
    arrowheads: List[Tuple[float, float, float, float]],
    W_pdf: float,
    H_pdf: float,
    sx: float,
    sy: float,
) -> Tuple[List[List[Tuple[int, int]]], List[List[Tuple[int, int]]], List[List[Tuple[int, int]]]]:
    """
    Group consecutive `lw ≈ 0.72` dash segments into contiguous polylines and
    classify them into:
    - `plan_missed_chains`: Curved/segmented Missed Approach escape tracks in the Plan View
    - `holding_chains`: Small semicircular U-turn ends of the Holding Pattern in the Plan View
    - `profile_missed_chains`: Curved dashed Missed Approach climb arrows in the Profile View
    """
    if not raw_072_dashes:
        return [], [], []

    chains: List[List[Tuple[int, float, float]]] = []
    for idx, mx, my, _, _ in raw_072_dashes:
        if not chains:
            chains.append([(idx, mx, my)])
        else:
            prev_idx, pmx, pmy = chains[-1][-1]
            if idx - prev_idx <= 3 and math.hypot(mx - pmx, my - pmy) <= 9.0:
                chains[-1].append((idx, mx, my))
            else:
                chains.append([(idx, mx, my)])

    plan_missed_chains: List[List[Tuple[int, int]]] = []
    holding_chains: List[List[Tuple[int, int]]] = []
    profile_missed_chains: List[List[Tuple[int, int]]] = []

    for ch in chains:
        if len(ch) < 6:
            continue
        pts_pdf = [( item[1], item[2] ) for item in ch]
        xs = [p[0] for p in pts_pdf]
        ys = [p[1] for p in pts_pdf]
        span = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
        cy_mean = sum(ys) / len(ys)

        # Attach any filled black arrowhead within 6 pt of either end of the dash chain
        for ax0, ay0, ax1, ay1 in arrowheads:
            acx, acy = (ax0 + ax1) / 2.0, (ay0 + ay1) / 2.0
            if math.hypot(acx - pts_pdf[0][0], acy - pts_pdf[0][1]) <= 6.5:
                pts_pdf.insert(0, (acx, acy))
                break
            if math.hypot(acx - pts_pdf[-1][0], acy - pts_pdf[-1][1]) <= 6.5:
                pts_pdf.append((acx, acy))
                break

        pts_px = [(int(round(px * sx)), int(round(py * sy))) for px, py in pts_pdf]

        if 0.635 * H_pdf <= cy_mean <= 0.84 * H_pdf:
            if len(ch) >= 8:
                profile_missed_chains.append(pts_px)
        elif 0.19 * H_pdf <= cy_mean <= 0.63 * H_pdf:
            # Holding pattern U-turns have 7..12 dashes and compact span (< 19 pt)
            if len(ch) <= 12 and span < 19.5:
                holding_chains.append(pts_px)
            else:
                plan_missed_chains.append(pts_px)

    return plan_missed_chains, holding_chains, profile_missed_chains
