"""
Non-Destructive RGBA Rounded-Rectangle, Morphological Flight-Path Ribbon,
Rotated Text Quad, MSA Ring & Vector PDF Overlay Renderer (`enhancer/compositor.py`).

CRITICAL SAFETY REQUIREMENT:
Never use generative AI image models, image-to-image translation, or text
re-rendering that alters, blurs, or overwrites the underlying chart pixels or
PDF vectors. The base image/PDF MUST remain 100% untouched as an immutable
base layer. All highlights are drawn on a separate transparent RGBA layer
(`PIL.Image.new("RGBA", size, (0, 0, 0, 0))`) and alpha-composited
(`Image.alpha_composite()`) strictly on top of the locked base image (or
inserted as a transparent overlay layer over the untouched vector PDF page).
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import List, Tuple, Union

import cv2
import numpy as np
import pymupdf
from PIL import Image, ImageDraw

from enhancer.rules import HighlightBox, PALETTE, SOLID_BORDER_COLORS


def _paste_rgba_crop(
    layer: Image.Image,
    crop_rgba: Image.Image,
    x0: int,
    y0: int,
) -> None:
    """Alpha-composite `crop_rgba` onto `layer` at `(x0, y0)`."""
    sub = layer.crop((x0, y0, x0 + crop_rgba.width, y0 + crop_rgba.height))
    blended = Image.alpha_composite(sub, crop_rgba)
    layer.paste(blended, (x0, y0))


def _render_polyline_ribbon(
    layer: Image.Image,
    points: List[Tuple[int, int]],
    radius: int,
    fill_rgba: Tuple[int, int, int, int],
    border_rgba: Tuple[int, int, int, int],
    border_width: int = 2,
) -> None:
    """
    Render a smooth, anti-aliased translucent ribbon (with uniform interior alpha
    and a crisp outer border ring) along an arbitrary straight or curved polyline
    `points` using 2x supersampled morphological masks.
    """
    if not points:
        return
    W, H = layer.size
    r = max(6, int(radius))
    bw = max(1, int(border_width))
    margin = r + bw + 6

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0 = max(0, min(xs) - margin)
    y0 = max(0, min(ys) - margin)
    x1 = min(W, max(xs) + margin)
    y1 = min(H, max(ys) + margin)
    cw, ch = x1 - x0, y1 - y0
    if cw <= 2 or ch <= 2:
        return

    # 2x supersampled binary mask for crisp anti-aliased edges
    mask_2x = np.zeros((ch * 2, cw * 2), dtype=np.uint8)
    pts_2x = np.array([[(px - x0) * 2, (py - y0) * 2] for px, py in points], dtype=np.int32)

    if len(pts_2x) == 1:
        cv2.circle(mask_2x, (int(pts_2x[0][0]), int(pts_2x[0][1])), r * 2, 255, -1, lineType=cv2.LINE_AA)
    else:
        cv2.polylines(
            mask_2x,
            [pts_2x],
            isClosed=False,
            color=255,
            thickness=r * 4,
            lineType=cv2.LINE_AA,
        )
        for pt in pts_2x:
            cv2.circle(mask_2x, (int(pt[0]), int(pt[1])), r * 2, 255, -1, lineType=cv2.LINE_AA)

    inner_2x = (mask_2x > 110).astype(np.uint8) * 255
    k_size = bw * 4 + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_size, k_size))
    outer_2x = cv2.dilate(inner_2x, kernel)
    border_2x = cv2.subtract(outer_2x, inner_2x)

    inner_aa = cv2.resize(inner_2x, (cw, ch), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    border_aa = cv2.resize(border_2x, (cw, ch), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0

    crop_arr = np.zeros((ch, cw, 4), dtype=np.uint8)
    fr, fg, fb, fa = fill_rgba
    br, bg, bb, ba = border_rgba

    # Fill interior
    fill_a = (inner_aa * fa).astype(np.uint8)
    has_fill = fill_a > 0
    crop_arr[has_fill, 0] = fr
    crop_arr[has_fill, 1] = fg
    crop_arr[has_fill, 2] = fb
    crop_arr[has_fill, 3] = fill_a[has_fill]

    # Overlay crisp border ring
    bord_a = (border_aa * ba).astype(np.uint8)
    has_bord = bord_a > 12
    crop_arr[has_bord, 0] = br
    crop_arr[has_bord, 1] = bg
    crop_arr[has_bord, 2] = bb
    crop_arr[has_bord, 3] = np.maximum(crop_arr[has_bord, 3], bord_a[has_bord])

    _paste_rgba_crop(layer, Image.fromarray(crop_arr, mode="RGBA"), x0, y0)


def _render_rotated_quad(
    layer: Image.Image,
    quad_points: List[Tuple[int, int]],
    padding: int,
    fill_rgba: Tuple[int, int, int, int],
    border_rgba: Tuple[int, int, int, int],
    border_width: int = 2,
) -> None:
    """
    Render a rotated rounded-corner quadrilateral around an angled text label
    (`quad_points` = 4 corners `[UL, UR, LR, LL]`).
    """
    if len(quad_points) < 3:
        return
    W, H = layer.size
    pad = max(2, int(padding))
    bw = max(1, int(border_width))
    margin = pad + bw + 6

    xs = [p[0] for p in quad_points]
    ys = [p[1] for p in quad_points]
    x0 = max(0, min(xs) - margin)
    y0 = max(0, min(ys) - margin)
    x1 = min(W, max(xs) + margin)
    y1 = min(H, max(ys) + margin)
    cw, ch = x1 - x0, y1 - y0
    if cw <= 2 or ch <= 2:
        return

    mask_2x = np.zeros((ch * 2, cw * 2), dtype=np.uint8)
    pts_2x = np.array([[(px - x0) * 2, (py - y0) * 2] for px, py in quad_points], dtype=np.int32)
    cv2.fillPoly(mask_2x, [pts_2x], 255, lineType=cv2.LINE_AA)

    if pad > 0:
        pad_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (pad * 4 + 1, pad * 4 + 1))
        mask_2x = cv2.dilate(mask_2x, pad_k)

    inner_2x = (mask_2x > 110).astype(np.uint8) * 255
    k_size = bw * 4 + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_size, k_size))
    outer_2x = cv2.dilate(inner_2x, kernel)
    border_2x = cv2.subtract(outer_2x, inner_2x)

    inner_aa = cv2.resize(inner_2x, (cw, ch), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    border_aa = cv2.resize(border_2x, (cw, ch), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0

    crop_arr = np.zeros((ch, cw, 4), dtype=np.uint8)
    fr, fg, fb, fa = fill_rgba
    br, bg, bb, ba = border_rgba

    fill_a = (inner_aa * fa).astype(np.uint8)
    has_fill = fill_a > 0
    crop_arr[has_fill, 0] = fr
    crop_arr[has_fill, 1] = fg
    crop_arr[has_fill, 2] = fb
    crop_arr[has_fill, 3] = fill_a[has_fill]

    bord_a = (border_aa * ba).astype(np.uint8)
    has_bord = bord_a > 12
    crop_arr[has_bord, 0] = br
    crop_arr[has_bord, 1] = bg
    crop_arr[has_bord, 2] = bb
    crop_arr[has_bord, 3] = np.maximum(crop_arr[has_bord, 3], bord_a[has_bord])

    _paste_rgba_crop(layer, Image.fromarray(crop_arr, mode="RGBA"), x0, y0)


def render_highlights(
    base_image: Image.Image,
    highlights: List[HighlightBox],
    opacity_scale: float = 1.0,
    corner_radius: int = 6,
) -> Tuple[Image.Image, Image.Image]:
    """
    Render `highlights` onto a brand-new transparent RGBA overlay layer and
    alpha-composite it strictly over an untouched RGBA view of `base_image`.

    Supports:
    - `rounded_rect`: Translucent rounded rectangular callout box
    - `quad`: Rotated rounded-corner quadrilateral around angled text callouts
    - `corridor` / `dot_path`: Smooth morphological flight-path & curved missed-approach ribbon
    - `circle`: MSA 25 NM circumference ring + border
    """
    locked_base = base_image.convert("RGBA")
    W, H = locked_base.size

    master_overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    for color_key in ("CYAN", "MAGENTA", "AMBER", "RED"):
        color_boxes = [b for b in highlights if b.color_key == color_key]
        if not color_boxes:
            continue

        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer, "RGBA")

        base_r, base_g, base_b, base_a = PALETTE[color_key]
        fill_alpha = max(15, min(200, int(round(base_a * opacity_scale))))
        fill_rgba = (base_r, base_g, base_b, fill_alpha)

        br, bg, bb, ba = SOLID_BORDER_COLORS[color_key]
        border_alpha = max(40, min(255, int(round(ba * min(1.25, opacity_scale)))))
        border_rgba = (br, bg, bb, border_alpha)

        for box in color_boxes:
            if box.shape in ("corridor", "dot_path", "polyline") and box.points and len(box.points) >= 2:
                _render_polyline_ribbon(
                    layer=layer,
                    points=box.points,
                    radius=max(8, box.padding),
                    fill_rgba=fill_rgba,
                    border_rgba=border_rgba,
                    border_width=max(2, box.border_width),
                )
                continue

            if box.shape == "quad" and box.points and len(box.points) >= 3:
                _render_rotated_quad(
                    layer=layer,
                    quad_points=box.points,
                    padding=max(3, box.padding),
                    fill_rgba=fill_rgba,
                    border_rgba=border_rgba,
                    border_width=max(2, box.border_width),
                )
                continue

            pad = box.padding
            x1 = max(0, box.x_min - pad)
            y1 = max(0, box.y_min - pad)
            x2 = min(W - 1, box.x_max + pad)
            y2 = min(H - 1, box.y_max + pad)
            if x2 <= x1 or y2 <= y1:
                continue

            if box.shape == "circle":
                ring_width = max(10, int(min(x2 - x1, y2 - y1) * 0.085))
                ring_rgba = (base_r, base_g, base_b, min(220, int(fill_alpha * 1.25)))
                draw.ellipse(
                    [x1, y1, x2, y2],
                    fill=None,
                    outline=ring_rgba,
                    width=ring_width,
                )
                draw.ellipse(
                    [x1, y1, x2, y2],
                    fill=None,
                    outline=border_rgba,
                    width=max(2, box.border_width),
                )
            else:
                rad = min(corner_radius, max(1, (x2 - x1) // 4), max(1, (y2 - y1) // 4))
                draw.rounded_rectangle(
                    [x1, y1, x2, y2],
                    radius=rad,
                    fill=fill_rgba,
                    outline=border_rgba,
                    width=max(1, box.border_width),
                )

        master_overlay = Image.alpha_composite(master_overlay, layer)

    composited = Image.alpha_composite(locked_base, master_overlay)
    return composited, master_overlay


def export_enhanced_pdf_bytes(
    pdf_source: Union[str, Path, bytes],
    overlay_image: Image.Image,
    page_index: int = 0,
) -> bytes:
    """
    Insert `overlay_image` (transparent RGBA highlight layer) strictly on top
    of the original vector FAA PDF page (`overlay=True`), keeping 100% of the
    underlying vector PDF lines, curves, and text completely untouched.
    """
    if isinstance(pdf_source, (bytes, bytearray)):
        doc = pymupdf.open(stream=bytes(pdf_source), filetype="pdf")
    else:
        doc = pymupdf.open(str(pdf_source))

    try:
        page = doc[page_index]
        buf = io.BytesIO()
        overlay_image.save(buf, format="PNG")
        page.insert_image(page.rect, stream=buf.getvalue(), overlay=True)
        return doc.tobytes(deflate=True)
    finally:
        doc.close()


def verify_base_pixel_integrity(
    original_image: Image.Image,
    composited_image: Image.Image,
    overlay_image: Image.Image,
) -> bool:
    """
    Safety verification utility:
    Asserts that every single pixel where `overlay_image` has alpha == 0 is
    100% bit-for-bit identical between `original_image.convert("RGBA")` and
    `composited_image`.
    """
    orig_arr = np.array(original_image.convert("RGBA"))
    comp_arr = np.array(composited_image.convert("RGBA"))
    over_arr = np.array(overlay_image.convert("RGBA"))

    if orig_arr.shape != comp_arr.shape:
        return False

    unhighlighted_mask = over_arr[:, :, 3] == 0
    return bool(np.array_equal(orig_arr[unhighlighted_mask], comp_arr[unhighlighted_mask]))
