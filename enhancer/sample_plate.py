"""
Synthetic FAA Instrument Approach Plate (IAP) Raster Generator for Testing & Demo.

Generates a high-resolution, standards-accurate raster PNG of an FAA IAP plate
(e.g., RNAV (GPS) RWY 32) with crisp black grid borders, briefing strip cells,
communications strip, plan view with MSA circle, obstacles, and holding fix,
profile view with 3 procedural missed approach icons and GP/TCH box, and the
bottom minimums table across Categories A, B, C, and D.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    ]
    for path in font_candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def create_sample_faa_plate(width: int = 1200, height: int = 1600) -> Image.Image:
    """
    Build a realistic, clean FAA IAP raster PNG in RGBA mode so users can test
    the deterministic 4-Color Tactical Briefing pipeline out-of-the-box.
    """
    img = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    draw = ImageDraw.Draw(img)

    W, H = width, height
    f_title = _load_font(max(16, int(H * 0.017)))
    f_lg = _load_font(max(15, int(H * 0.015)))
    f_md = _load_font(max(13, int(H * 0.0125)))
    f_sm = _load_font(max(11, int(H * 0.0105)))

    black = (0, 0, 0, 255)
    gray_line = (40, 40, 40, 255)

    # Outer border
    m_left = int(0.045 * W)
    m_right = int(0.955 * W)
    m_top = int(0.055 * H)
    m_bot = int(0.945 * H)
    draw.rectangle([m_left, m_top, m_right, m_bot], outline=black, width=3)

    # Header Title (y < 0.055 * H)
    draw.text((m_left, int(0.025 * H)), "SANTA MONICA, CALIFORNIA", fill=black, font=f_md)
    draw.text((int(0.62 * W), int(0.022 * H)), "RNAV (GPS) RWY 32", fill=black, font=f_title)

    # 1. Top Row Briefing Strip: APP CRS | Rwy Idg / TDZE / Apt Elev | MISSED APPROACH
    # Zone: 0.055 * H to 0.145 * H
    y_brief_top = m_top
    y_brief_mid = int(0.105 * H)
    y_brief_bot = int(0.180 * H)

    # APP CRS Box (x < 0.30 * W, y < 0.12 * H)
    x_crs_right = int(0.20 * W)
    x_apt_right = int(0.48 * W)
    draw.rectangle([m_left, y_brief_top, x_crs_right, y_brief_mid], outline=black, width=2)
    draw.text((m_left + 18, y_brief_top + 10), "APP CRS", fill=black, font=f_sm)
    draw.text((m_left + 25, y_brief_top + 36), "321°", fill=black, font=f_title)

    # Rwy Idg / TDZE / Apt Elev cell
    draw.rectangle([x_crs_right, y_brief_top, x_apt_right, y_brief_mid], outline=black, width=2)
    draw.text((x_crs_right + 12, y_brief_top + 8), "Rwy Idg   4973", fill=black, font=f_sm)
    draw.text((x_crs_right + 12, y_brief_top + 28), "TDZE       177", fill=black, font=f_sm)
    draw.text((x_crs_right + 12, y_brief_top + 48), "Apt Elev   177", fill=black, font=f_sm)

    # T / A Notes & Restrictions Box (0.08 * H < y < 0.18 * H, x < 0.50 * W)
    draw.rectangle([m_left, y_brief_mid, x_apt_right, y_brief_bot], outline=black, width=2)
    draw.text(
        (m_left + 12, y_brief_mid + 10),
        "Circling NA northeast of Rwy 14-32.",
        fill=black,
        font=f_sm,
    )
    draw.text(
        (m_left + 12, y_brief_mid + 34),
        "Baro-VNAV NA below -15°C.",
        fill=black,
        font=f_sm,
    )
    draw.text(
        (m_left + 12, y_brief_mid + 58),
        "Helicopter visibility reduction below 3/4 SM NA.",
        fill=black,
        font=f_sm,
    )

    # Textual Missed Approach Box (0.08 * H < y < 0.18 * H, x > 0.50 * W)
    draw.rectangle([x_apt_right, y_brief_top, m_right, y_brief_bot], outline=black, width=2)
    draw.text(
        (x_apt_right + 14, y_brief_mid - 15),
        "MISSED APPROACH: Climb to 3000 direct ACETY and hold.",
        fill=black,
        font=f_sm,
    )
    draw.text(
        (x_apt_right + 14, y_brief_mid + 15),
        "Continue climb on course 321 to 4000.",
        fill=black,
        font=f_sm,
    )
    draw.text(
        (x_apt_right + 14, y_brief_mid + 45),
        "* Missed approach requires minimum climb of 310 feet per NM to 2500.",
        fill=black,
        font=f_sm,
    )

    # 2. Communications Strip (0.180 * H to 0.215 * H -> inside 0.10*H < y < 0.22*H)
    y_comms_top = int(0.165 * H)
    y_comms_bot = int(0.215 * H)
    # Re-align notes bottom to y_comms_top so everything is cleanly gridded
    draw.rectangle([m_left, y_comms_top, m_right, y_comms_bot], fill=(255, 255, 255, 255), outline=black, width=2)

    comms_cols = [
        ("ATIS", "119.15"),
        ("SOCAL APP CON", "124.525"),
        ("SANTA MONICA TOWER", "120.1"),
        ("GND CON", "121.9"),
        ("CLNC DEL", "124.35"),
    ]
    col_w = (m_right - m_left) // len(comms_cols)
    for i, (fac, freq) in enumerate(comms_cols):
        cx1 = m_left + i * col_w
        cx2 = m_right if i == len(comms_cols) - 1 else (m_left + (i + 1) * col_w)
        draw.rectangle([cx1, y_comms_top, cx2, y_comms_bot], outline=black, width=2)
        draw.text((cx1 + 14, y_comms_top + 10), fac, fill=black, font=f_sm)
        draw.text((cx1 + 14, y_comms_top + 36), freq, fill=black, font=f_md)

    # 3. Plan View (0.215 * H < y < 0.635 * H)
    y_plan_top = y_comms_bot
    y_plan_bot = int(0.635 * H)
    draw.rectangle([m_left, y_plan_top, m_right, y_plan_bot], outline=black, width=2)

    # Plan View Arrival Restriction Note
    draw.text(
        (m_left + 30, int(0.25 * H)),
        "Procedure NA for arrival on VTU VOR/DME airway radials 090 CW 150.",
        fill=black,
        font=f_sm,
    )

    # MSA Circle in Plan View (top-right of Plan View)
    msa_cx = int(0.81 * W)
    msa_cy = int(0.32 * H)
    msa_r = int(0.062 * min(W, H))
    draw.ellipse(
        [msa_cx - msa_r, msa_cy - msa_r, msa_cx + msa_r, msa_cy + msa_r],
        outline=black,
        width=2,
    )
    draw.text((msa_cx - 58, msa_cy - msa_r - 22), "MSA RW32 25 NM", fill=black, font=f_sm)
    draw.text((msa_cx - 24, msa_cy - 10), "5600", fill=black, font=f_lg)

    # Plan View Missed Approach Holding Fix (ACETY)
    draw.text((int(0.20 * W), int(0.32 * H)), "MISSED APCH FIX", fill=black, font=f_sm)
    draw.text((int(0.20 * W), int(0.345 * H)), "ACETY", fill=black, font=f_lg)

    # Plan View Primary Course, IF, FAF & Waypoints
    draw.line(
        [(int(0.68 * W), int(0.56 * H)), (int(0.34 * W), int(0.39 * H))],
        fill=gray_line,
        width=3,
    )
    draw.text((int(0.62 * W), int(0.53 * H)), "(IF) DARTS", fill=black, font=f_md)
    draw.text((int(0.62 * W), int(0.555 * H)), "3600", fill=black, font=f_md)

    draw.text((int(0.47 * W), int(0.45 * H)), "(FAF) DUCKE", fill=black, font=f_md)
    draw.text((int(0.47 * W), int(0.475 * H)), "2100", fill=black, font=f_md)
    draw.text((int(0.40 * W), int(0.42 * H)), "321°", fill=black, font=f_md)

    draw.text((int(0.33 * W), int(0.38 * H)), "RW32", fill=black, font=f_md)

    # Plan View Spot Elevations / Obstacles (\b[2-9]\d{3}\b)
    draw.text((int(0.25 * W), int(0.52 * H)), "2845", fill=black, font=f_md)
    draw.text((int(0.74 * W), int(0.48 * H)), "3412", fill=black, font=f_md)

    # 4. Profile View (0.635 * H < y < 0.785 * H)
    y_prof_top = y_plan_bot
    y_prof_bot = int(0.785 * H)
    draw.rectangle([m_left, y_prof_top, m_right, y_prof_bot], outline=black, width=2)

    # 3 small procedural Missed Approach icon boxes at top-left of Profile View
    # Spatial Zone: 0.62 * H < y < 0.73 * H, x < 0.35 * W
    icon_y1 = int(0.642 * H)
    icon_y2 = int(0.692 * H)
    icon_w = int(0.075 * W)
    icon_texts = ["3000", "321°", "ACETY"]
    for idx, itxt in enumerate(icon_texts):
        ix1 = m_left + 10 + idx * (icon_w + 8)
        ix2 = ix1 + icon_w
        draw.rectangle([ix1, icon_y1, ix2, icon_y2], outline=black, width=2)
        draw.text((ix1 + 12, icon_y1 + 22), itxt, fill=black, font=f_sm)

    # Profile View Descent Slope, Course & Stepdown Crossing Altitudes
    draw.line(
        [(int(0.78 * W), int(0.67 * H)), (int(0.35 * W), int(0.765 * H))],
        fill=black,
        width=3,
    )
    draw.text((int(0.58 * W), int(0.675 * H)), "321°", fill=black, font=f_md)
    draw.text((int(0.76 * W), int(0.695 * H)), "3600", fill=black, font=f_md)
    draw.text((int(0.64 * W), int(0.715 * H)), "2700", fill=black, font=f_md)
    draw.text((int(0.50 * W), int(0.735 * H)), "2100", fill=black, font=f_md)
    draw.text((int(0.40 * W), int(0.750 * H)), "1700", fill=black, font=f_md)

    # Glidepath Angle (GP / TCH) Box in Profile View (with steep 4.00° > 3.00°)
    gp_x1, gp_y1 = int(0.18 * W), int(0.722 * H)
    gp_x2, gp_y2 = int(0.34 * W), int(0.772 * H)
    draw.rectangle([gp_x1, gp_y1, gp_x2, gp_y2], outline=black, width=2)
    draw.text((gp_x1 + 12, gp_y1 + 10), "GP 4.00°", fill=black, font=f_md)
    draw.text((gp_x1 + 12, gp_y1 + 36), "TCH 50", fill=black, font=f_sm)

    # 5. Minimums Table (Bottom Left: y > 0.785 * H, x < 0.68 * W)
    y_min_top = y_prof_bot
    x_min_right = int(0.665 * W)
    draw.rectangle([m_left, y_min_top, x_min_right, m_bot], outline=black, width=2)

    # Category Header Row
    y_cat_bot = int(0.815 * H)
    draw.line([(m_left, y_cat_bot), (x_min_right, y_cat_bot)], fill=black, width=2)
    x_label_col = int(0.225 * W)
    draw.line([(x_label_col, y_min_top), (x_label_col, m_bot)], fill=black, width=2)
    draw.text((m_left + 14, y_min_top + 12), "CATEGORY", fill=black, font=f_sm)

    cat_letters = ["A", "B", "C", "D"]
    cat_col_w = (x_min_right - x_label_col) // 4
    for i, c_let in enumerate(cat_letters):
        cx1 = x_label_col + i * cat_col_w
        if i > 0:
            draw.line([(cx1, y_min_top), (cx1, m_bot)], fill=black, width=2)
        draw.text((cx1 + cat_col_w // 2 - 6, y_min_top + 12), c_let, fill=black, font=f_md)

    min_rows = [
        ("LPV DA", ["427-1", "427-1", "427-1 1/4", "427-1 1/2"]),
        ("LNAV/VNAV DA", ["580-1 1/4", "580-1 1/4", "580-1 1/2", "580-1 3/4"]),
        ("LNAV MDA", ["660-1", "660-1", "660-1 3/4", "660-2"]),
        ("CIRCLING", ["740-1", "780-1", "860-2 1/4", "940-3"]),
    ]
    row_h = (m_bot - y_cat_bot) // len(min_rows)
    for r_idx, (r_title, r_vals) in enumerate(min_rows):
        ry1 = y_cat_bot + r_idx * row_h
        ry2 = m_bot if r_idx == len(min_rows) - 1 else (ry1 + row_h)
        draw.line([(m_left, ry2), (x_min_right, ry2)], fill=black, width=2)
        draw.text((m_left + 12, ry1 + 16), r_title, fill=black, font=f_sm)
        for c_idx, val in enumerate(r_vals):
            cx1 = x_label_col + c_idx * cat_col_w
            draw.text((cx1 + 12, ry1 + 16), val, fill=black, font=f_sm)

    return img
