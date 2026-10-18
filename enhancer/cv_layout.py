"""
OpenCV Structural Box, Table Grid Line, Flight Path Line & MSA Circle Detection (`cv_layout.py`).

Detects:
- Horizontal and vertical table grid lines & rectangular cells (briefing strip
  boxes, minimums table rows/columns, profile view procedural icon boxes)
- Plan View final approach course line segment (`plan_course_segment`)
- Profile View descent slope line segment (`profile_slope_segment`)
- Plan View dashed missed-approach track mask (`missed_track_points`)
- Exact Minimum Safe Altitude (MSA) circle circumference (`fit_msa_circle`)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image


@dataclass
class DetectedCircle:
    """Circle detected on the chart (e.g., MSA 25 NM circle)."""
    center_x: int
    center_y: int
    radius: int

    @property
    def bbox(self) -> Tuple[int, int, int, int]:
        return (
            self.center_x - self.radius,
            self.center_y - self.radius,
            self.center_x + self.radius,
            self.center_y + self.radius,
        )


@dataclass
class PlateLayout:
    """Structural layout and vector path features detected on the plate (via PDF vectors or OpenCV)."""
    W: int
    H: int
    gray: Optional[np.ndarray] = field(default=None, repr=False)
    horizontal_lines_y: List[int] = field(default_factory=list)
    vertical_lines_x: List[int] = field(default_factory=list)
    table_cells: List[Tuple[int, int, int, int]] = field(default_factory=list)
    profile_icon_boxes: List[Tuple[int, int, int, int]] = field(default_factory=list)
    circles: List[DetectedCircle] = field(default_factory=list)
    minimums_col_bounds: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    # Exact PDF vector path geometry (populated when input is a native FAA d-TPP PDF)
    is_pdf_vector: bool = False
    pdf_plan_course_groups: List[Tuple[int, int, int, int]] = field(default_factory=list)
    pdf_profile_slope_seg: Optional[Tuple[int, int, int, int]] = None
    pdf_plan_missed_chains: List[List[Tuple[int, int]]] = field(default_factory=list)
    pdf_profile_missed_chains: List[List[Tuple[int, int]]] = field(default_factory=list)
    pdf_holding_chains: List[List[Tuple[int, int]]] = field(default_factory=list)
    pdf_msa_circle: Optional[DetectedCircle] = None
    pdf_msa_inner_box: Optional[Tuple[int, int, int, int]] = None

    def snap_box_to_cell(
        self,
        bbox: Tuple[int, int, int, int],
        max_expansion_ratio: float = 0.05,
    ) -> Tuple[int, int, int, int]:
        """
        If `bbox` lies inside a detected rectangular cell whose dimensions are
        reasonably close, snap to the cell borders (with a 2px inset so black
        grid borders remain crisp).
        """
        x1, y1, x2, y2 = bbox
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        max_exp_x = self.W * max_expansion_ratio
        max_exp_y = self.H * max_expansion_ratio

        best_cell: Optional[Tuple[int, int, int, int]] = None
        best_area = float("inf")
        for cx1, cy1, cx2, cy2 in self.table_cells:
            if cx1 - 6 <= cx <= cx2 + 6 and cy1 - 6 <= cy <= cy2 + 6:
                if (
                    abs(cx1 - x1) <= max_exp_x
                    and abs(cx2 - x2) <= max_exp_x
                    and abs(cy1 - y1) <= max_exp_y
                    and abs(cy2 - y2) <= max_exp_y
                ):
                    area = (cx2 - cx1) * (cy2 - cy1)
                    if area < best_area and area > (x2 - x1) * (y2 - y1) * 0.7:
                        best_area = area
                        best_cell = (cx1 + 2, cy1 + 2, cx2 - 2, cy2 - 2)

        return best_cell if best_cell is not None else bbox

    def snap_minimums_row(
        self,
        row_y_center: float,
        row_y_min: int,
        row_y_max: int,
        categories: List[str],
    ) -> Tuple[int, int, int, int]:
        """
        Compute the exact horizontal row bounding box in the Minimums Table
        (`y > 0.76 * H`, `x < 0.68 * W`) spanning from the left table border
        to the rightmost selected Aircraft Category column (`A`, `B`, `C`, `D`).
        """
        min_zone_lines = [y for y in self.horizontal_lines_y if y >= int(0.75 * self.H)]
        y_top = row_y_min - 4
        y_bot = row_y_max + 4

        above = [y for y in min_zone_lines if y <= row_y_center - 3]
        below = [y for y in min_zone_lines if y >= row_y_center + 3]
        if above:
            candidate_top = max(above)
            if row_y_center - candidate_top <= 0.045 * self.H:
                y_top = candidate_top + 2
        if below:
            candidate_bot = min(below)
            if candidate_bot - row_y_center <= 0.045 * self.H:
                y_bot = candidate_bot - 2

        if "CATEGORY" in self.minimums_col_bounds:
            x_left = self.minimums_col_bounds["CATEGORY"][0] + 2
        else:
            left_verts = [
                x for x in self.vertical_lines_x if int(0.02 * self.W) <= x <= int(0.12 * self.W)
            ]
            x_left = min(left_verts) + 2 if left_verts else int(0.045 * self.W)

        norm_cats = [
            c.upper().strip()
            for c in categories
            if c.upper().strip() in ("A", "B", "C", "D")
        ]
        if not norm_cats:
            norm_cats = ["A", "B"]

        cat_order = ["A", "B", "C", "D"]
        max_cat = max(norm_cats, key=lambda c: cat_order.index(c))

        if max_cat in self.minimums_col_bounds:
            x_right = self.minimums_col_bounds[max_cat][1] - 2
        else:
            default_right_ratios = {
                "A": 0.335,
                "B": 0.445,
                "C": 0.555,
                "D": 0.665,
            }
            target_x = int(default_right_ratios[max_cat] * self.W)
            near_verts = [
                vx for vx in self.vertical_lines_x if abs(vx - target_x) <= int(0.035 * self.W)
            ]
            x_right = (
                min(near_verts, key=lambda vx: abs(vx - target_x)) - 2
                if near_verts
                else target_x
            )

        return (x_left, y_top, max(x_left + 20, x_right), max(y_top + 8, y_bot))

    def fit_msa_circle(self, alt_x: int, alt_y: int) -> DetectedCircle:
        """
        Locate the exact MSA circle circumference around a detected 4-digit MSA
        altitude box `(alt_x, alt_y)`. Uses exact PDF Bezier circle if available.
        """
        if self.pdf_msa_circle is not None:
            return self.pdf_msa_circle

        if self.gray is None:
            r_def = int(0.078 * self.W)
            return DetectedCircle(center_x=alt_x, center_y=alt_y + int(r_def * 0.55), radius=r_def)

        ink = (self.gray < 145).astype(np.float32)
        angles = np.linspace(0, 2.0 * np.pi, 120, endpoint=False)
        cos_a, sin_a = np.cos(angles), np.sin(angles)

        best_score = -1.0
        best_c = (alt_x, alt_y + int(0.045 * self.H), int(0.08 * self.W))

        r_min = max(35, int(self.W * 0.058))
        r_max = max(r_min + 10, int(self.W * 0.096))

        for r in range(r_min, r_max, 2):
            for cy in range(max(r, alt_y - 12), min(self.H - r, alt_y + int(r * 1.05)), 3):
                for cx in range(max(r, alt_x - int(r * 0.45)), min(self.W - r, alt_x + int(r * 0.45)), 3):
                    xs = np.clip((cx + r * cos_a).astype(int), 0, self.W - 1)
                    ys = np.clip((cy + r * sin_a).astype(int), 0, self.H - 1)
                    score = float(np.mean(ink[ys, xs]))
                    if score > best_score:
                        best_score = score
                        best_c = (cx, cy, r)

        if best_score < 0.16 and self.circles:
            for hc in self.circles:
                if np.hypot(hc.center_x - alt_x, hc.center_y - alt_y) <= hc.radius * 1.4:
                    return hc

        return DetectedCircle(center_x=best_c[0], center_y=best_c[1], radius=best_c[2])

    def detect_profile_descent_segment(
        self,
        min_x_hint: Optional[int] = None,
        max_x_hint: Optional[int] = None,
    ) -> Optional[Tuple[int, int, int, int]]:
        """
        Detect the angled final-approach descent slope line in the Profile View.
        Uses exact PDF vector descent slope segments if available.
        """
        if self.pdf_profile_slope_seg is not None:
            return self.pdf_profile_slope_seg

        if self.gray is None:
            return None

        y_off = int(0.64 * self.H)
        y_end = int(0.775 * self.H)
        x_off = int(0.15 * self.W)
        x_end = int(0.82 * self.W)

        roi = (self.gray[y_off:y_end, x_off:x_end] < 140).astype(np.uint8) * 255
        lines = cv2.HoughLinesP(
            roi, 1, np.pi / 180.0, threshold=35, minLineLength=55, maxLineGap=25
        )
        if lines is None:
            return None

        best_seg: Optional[Tuple[int, int, int, int]] = None
        best_len = 0.0

        for l in lines.reshape(-1, 4):
            lx1, ly1, lx2, ly2 = int(l[0]) + x_off, int(l[1]) + y_off, int(l[2]) + x_off, int(l[3]) + y_off
            dx, dy = lx2 - lx1, ly2 - ly1
            ang = abs(np.degrees(np.arctan2(dy, dx)))
            if 8.0 <= ang <= 26.0:
                seg_len = float(np.hypot(dx, dy))
                if seg_len > best_len:
                    best_len = seg_len
                    best_seg = (lx1, ly1, lx2, ly2)

        if best_seg is None:
            return None

        sx1, sy1, sx2, sy2 = best_seg
        if sx1 > sx2:
            sx1, sy1, sx2, sy2 = sx2, sy2, sx1, sy1

        if min_x_hint is not None or max_x_hint is not None:
            slope = (sy2 - sy1) / max(1.0, float(sx2 - sx1))
            nx1 = max(sx1, min_x_hint) if min_x_hint is not None else sx1
            nx2 = min(sx2, max_x_hint) if max_x_hint is not None else sx2
            if nx2 - nx1 >= 60:
                ny1 = int(round(sy1 + slope * (nx1 - sx1)))
                ny2 = int(round(sy1 + slope * (nx2 - sx1)))
                return (nx1, ny1, nx2, ny2)

        return (sx1, sy1, sx2, sy2)

    def detect_plan_course_segments(
        self,
        anchor_points: List[Tuple[float, float]],
    ) -> List[Tuple[int, int, int, int]]:
        """
        Detect all connected final and intermediate approach course segments in
        the Plan View (including dogleg segments where the course turns slightly
        at the FAF, such as `303° -> 312°` on `RHV` or `219° -> 213°` on `KSMO`).
        """
        primary = self.detect_plan_course_segment(anchor_points)
        if primary is None:
            return []
        if not self.pdf_plan_course_groups:
            return [primary]

        selected = [primary]
        px1, py1, px2, py2 = primary
        p_ang = float(np.degrees(np.arctan2(py2 - py1, px2 - px1))) % 180.0

        for g in self.pdf_plan_course_groups:
            if g == primary:
                continue
            gx1, gy1, gx2, gy2 = g
            g_ang = float(np.degrees(np.arctan2(gy2 - gy1, gx2 - gx1))) % 180.0
            adiff = min(abs(g_ang - p_ang), 180.0 - abs(g_ang - p_ang))
            if adiff <= 20.0:
                # Check if an endpoint of g meets an endpoint of any selected segment within ~0.032*W
                end_dist = min(
                    float(np.hypot(gx1 - sx, gy1 - sy))
                    for (sx1, sy1, sx2, sy2) in selected
                    for (sx, sy) in ((sx1, sy1), (sx2, sy2))
                )
                end_dist = min(
                    end_dist,
                    min(
                        float(np.hypot(gx2 - sx, gy2 - sy))
                        for (sx1, sy1, sx2, sy2) in selected
                        for (sx, sy) in ((sx1, sy1), (sx2, sy2))
                    ),
                )
                if end_dist <= 0.032 * self.W:
                    selected.append(g)

        return selected

    def detect_plan_course_segment(
        self,
        anchor_points: List[Tuple[float, float]],
    ) -> Optional[Tuple[int, int, int, int]]:
        """
        Detect the primary approach course line segment in the Plan View passing
        near the runway / FAF / IF `anchor_points`.
        Uses exact PDF vector course groups (`pdf_plan_course_groups`) when available.
        """
        if self.pdf_plan_course_groups:
            if not anchor_points:
                return max(
                    self.pdf_plan_course_groups,
                    key=lambda g: float(np.hypot(g[2] - g[0], g[3] - g[1])),
                )
            best_g: Optional[Tuple[int, int, int, int]] = None
            best_g_score = -1e9
            for gx1, gy1, gx2, gy2 in self.pdf_plan_course_groups:
                gdx, gdy = gx2 - gx1, gy2 - gy1
                glen = float(np.hypot(gdx, gdy))
                if glen < 10.0:
                    continue
                near_cnt = 0
                dist_sum = 0.0
                for ax, ay in anchor_points:
                    t = ((ax - gx1) * gdx + (ay - gy1) * gdy) / max(1.0, glen * glen)
                    t_c = max(-0.2, min(1.2, t))
                    px = gx1 + t_c * gdx
                    py = gy1 + t_c * gdy
                    d = float(np.hypot(ax - px, ay - py))
                    if d <= 0.09 * self.W:
                        near_cnt += 1
                        dist_sum += d
                score = near_cnt * 500.0 + glen - dist_sum * 0.8
                if score > best_g_score:
                    best_g_score = score
                    best_g = (gx1, gy1, gx2, gy2)
            return best_g
        if self.gray is None or not anchor_points:
            return None

        y_off = int(0.22 * self.H)
        y_end = int(0.61 * self.H)
        x_off = int(0.20 * self.W)
        x_end = int(0.80 * self.W)

        roi = (self.gray[y_off:y_end, x_off:x_end] < 110).astype(np.uint8) * 255
        lines = cv2.HoughLinesP(
            roi, 1, np.pi / 180.0, threshold=40, minLineLength=65, maxLineGap=30
        )
        if lines is None:
            return None

        best_seg: Optional[Tuple[int, int, int, int]] = None
        best_score = -1.0

        for l in lines.reshape(-1, 4):
            lx1, ly1, lx2, ly2 = int(l[0]) + x_off, int(l[1]) + y_off, int(l[2]) + x_off, int(l[3]) + y_off
            dx, dy = lx2 - lx1, ly2 - ly1
            seg_len = float(np.hypot(dx, dy))
            if seg_len < 85:
                continue
            ang = abs(np.degrees(np.arctan2(dy, dx)))
            if ang < 12.0 or ang > 88.0:
                continue

            # Measure perpendicular distance from anchor points (e.g. RWY, FAF, IF) to this line
            near_anchors = 0
            min_dist_sum = 0.0
            for ax, ay in anchor_points:
                # Distance from point (ax, ay) to segment (lx1, ly1)-(lx2, ly2)
                t = ((ax - lx1) * dx + (ay - ly1) * dy) / max(1.0, seg_len * seg_len)
                t_clamped = max(-0.15, min(1.15, t))
                px = lx1 + t_clamped * dx
                py = ly1 + t_clamped * dy
                d = float(np.hypot(ax - px, ay - py))
                if d <= 0.075 * self.W:
                    near_anchors += 1
                    min_dist_sum += d

            if near_anchors >= 1:
                score = seg_len + near_anchors * 120.0 - min_dist_sum * 0.5
                if score > best_score:
                    best_score = score
                    best_seg = (lx1, ly1, lx2, ly2)

        if best_seg is not None and len(anchor_points) >= 2:
            bx1, by1, bx2, by2 = best_seg
            bdx, bdy = bx2 - bx1, by2 - by1
            blen2 = float(bdx * bdx + bdy * bdy)
            if blen2 > 1.0:
                ts: List[float] = []
                for ax, ay in anchor_points:
                    t = ((ax - bx1) * bdx + (ay - by1) * bdy) / blen2
                    px = bx1 + max(0.0, min(1.0, t)) * bdx
                    py = by1 + max(0.0, min(1.0, t)) * bdy
                    if np.hypot(ax - px, ay - py) <= 0.08 * self.W:
                        ts.append(max(0.0, min(1.0, t)))
                if len(ts) >= 2 and (max(ts) - min(ts)) >= 0.35:
                    t_min = max(0.0, min(ts) - 0.04)
                    t_max = min(1.0, max(ts) + 0.04)
                    best_seg = (
                        int(round(bx1 + t_min * bdx)),
                        int(round(by1 + t_min * bdy)),
                        int(round(bx1 + t_max * bdx)),
                        int(round(by1 + t_max * bdy)),
                    )

        return best_seg

    def detect_plan_missed_track_dots(
        self,
        rwy_point: Tuple[float, float],
    ) -> List[Tuple[int, int]]:
        """
        Detect the dashed missed-approach escape path near the runway threshold
        in the Plan View by finding clusters of small black dash/dot connected
        components originating near `rwy_point`.
        """
        if self.gray is None:
            return []

        rx, ry = int(rwy_point[0]), int(rwy_point[1])
        pad_x = int(0.12 * self.W)
        pad_y = int(0.08 * self.H)
        x1 = max(int(0.05 * self.W), rx - pad_x)
        x2 = min(int(0.95 * self.W), rx + pad_x)
        y1 = max(int(0.205 * self.H), ry - pad_y)
        y2 = min(int(0.60 * self.H), ry + pad_y)
        if x2 <= x1 or y2 <= y1:
            return []

        roi_ink = (self.gray[y1:y2, x1:x2] < 95).astype(np.uint8) * 255
        num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(roi_ink)

        dot_centers: List[Tuple[int, int]] = []
        for i in range(1, num_labels):
            cx_box, cy_box, cw, ch, area = stats[i]
            # Dashed missed approach track segments are small compact black dashes/dots
            if 6 <= area <= 65 and cw <= 16 and ch <= 16:
                aspect = max(cw, ch) / max(1.0, float(min(cw, ch)))
                if aspect <= 3.5:
                    gx = int(round(centroids[i][0])) + x1
                    gy = int(round(centroids[i][1])) + y1
                    dot_centers.append((gx, gy))

        if len(dot_centers) < 8:
            return []

        # Keep only dots that have at least 2 neighboring dots within 22px (forming a dashed curve)
        pts = np.array(dot_centers)
        curve_dots: List[Tuple[int, int]] = []
        for idx, (px, py) in enumerate(dot_centers):
            dists = np.hypot(pts[:, 0] - px, pts[:, 1] - py)
            neighbors = int(np.sum((dists > 0) & (dists <= 22.0)))
            if neighbors >= 2:
                curve_dots.append((px, py))

        return curve_dots if len(curve_dots) >= 8 else []


def _cluster_1d(coords: List[int], min_gap: int = 5) -> List[int]:
    if not coords:
        return []
    coords_sorted = sorted(set(coords))
    clusters: List[List[int]] = [[coords_sorted[0]]]
    for c in coords_sorted[1:]:
        if c - clusters[-1][-1] <= min_gap:
            clusters[-1].append(c)
        else:
            clusters.append([c])
    return [int(round(sum(cl) / len(cl))) for cl in clusters]


def detect_plate_layout(image: Image.Image) -> PlateLayout:
    """
    Use OpenCV morphological line detection, contour analysis, and HoughCircles
    on an in-memory copy of `image` to extract structural boxes and lines.
    """
    W, H = image.size
    rgb = np.array(image.convert("RGB"), copy=True)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    _, binary_inv = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)

    # 1. Horizontal line detection
    horiz_kernel_len = max(25, int(W * 0.06))
    horiz_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (horiz_kernel_len, 1))
    horiz_mask = cv2.morphologyEx(binary_inv, cv2.MORPH_OPEN, horiz_kernel, iterations=1)

    horiz_proj = np.sum(horiz_mask > 0, axis=1)
    raw_h_lines = [int(y) for y in range(H) if horiz_proj[y] >= max(30, int(W * 0.08))]
    horizontal_lines_y = _cluster_1d(raw_h_lines, min_gap=max(3, int(H * 0.004)))

    # 2. Vertical line detection
    vert_kernel_len = max(18, int(H * 0.025))
    vert_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, vert_kernel_len))
    vert_mask = cv2.morphologyEx(binary_inv, cv2.MORPH_OPEN, vert_kernel, iterations=1)

    vert_proj = np.sum(vert_mask > 0, axis=0)
    raw_v_lines = [int(x) for x in range(W) if vert_proj[x] >= max(20, int(H * 0.03))]
    vertical_lines_x = _cluster_1d(raw_v_lines, min_gap=max(3, int(W * 0.004)))

    # 3. Rectangular table cells & profile view missed approach icon boxes
    grid_mask = cv2.bitwise_or(horiz_mask, vert_mask)
    contours, _ = cv2.findContours(grid_mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

    table_cells: List[Tuple[int, int, int, int]] = []
    profile_icon_boxes: List[Tuple[int, int, int, int]] = []

    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        if cw < 15 or ch < 10:
            continue
        if cw > W * 0.92 and ch > H * 0.85:
            continue

        table_cells.append((x, y, x + cw, y + ch))

        # 3 small procedural missed-approach icon boxes in the top-left of Profile View
        cy = y + ch / 2.0
        cx = x + cw / 2.0
        if (
            0.61 * H <= cy <= 0.74 * H
            and cx <= 0.36 * W
            and 0.025 * W <= cw <= 0.16 * W
            and 0.015 * H <= ch <= 0.085 * H
        ):
            profile_icon_boxes.append((x, y, x + cw, y + ch))

    profile_icon_boxes = _dedup_boxes(profile_icon_boxes)

    # 4. Detect candidate circles via cv2.HoughCircles
    circles: List[DetectedCircle] = []
    blurred = cv2.GaussianBlur(gray, (5, 5), 1.2)
    min_r = max(18, int(min(W, H) * 0.035))
    max_r = max(min_r + 10, int(min(W, H) * 0.11))
    detected = cv2.HoughCircles(
        blurred,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=max(30, int(min(W, H) * 0.08)),
        param1=100,
        param2=38,
        minRadius=min_r,
        maxRadius=max_r,
    )
    if detected is not None:
        for c in np.round(detected[0, :]).astype("int"):
            cx, cy, r = int(c[0]), int(c[1]), int(c[2])
            circles.append(DetectedCircle(center_x=cx, center_y=cy, radius=r))

    return PlateLayout(
        W=W,
        H=H,
        gray=gray,
        horizontal_lines_y=horizontal_lines_y,
        vertical_lines_x=vertical_lines_x,
        table_cells=table_cells,
        profile_icon_boxes=profile_icon_boxes[:3],
        circles=circles,
    )


def _dedup_boxes(boxes: List[Tuple[int, int, int, int]]) -> List[Tuple[int, int, int, int]]:
    if not boxes:
        return []
    boxes_sorted = sorted(boxes, key=lambda b: (b[0], b[1]))
    kept: List[Tuple[int, int, int, int]] = []
    for b in boxes_sorted:
        x1, y1, x2, y2 = b
        dup = False
        for k in kept:
            kx1, ky1, kx2, ky2 = k
            ix1, iy1 = max(x1, kx1), max(y1, ky1)
            ix2, iy2 = min(x2, kx2), min(y2, ky2)
            if ix2 > ix1 and iy2 > iy1:
                inter = (ix2 - ix1) * (iy2 - iy1)
                area = max(1, (x2 - x1) * (y2 - y1))
                if inter / area > 0.6:
                    dup = True
                    break
        if not dup:
            kept.append(b)
    return kept
