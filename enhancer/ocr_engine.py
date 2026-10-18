"""
OCR Pre-Processing, Bounding-Box Extraction & Horizontal Line Grouping.

Safety-Critical Requirement:
All pre-processing (2x upscale, grayscale, adaptive/binary thresholding)
occurs strictly on an in-memory NumPy copy. The original PIL Image is never
mutated or altered. Bounding boxes are mapped back to original (W, H) pixel
coordinates and normalized to [0.0, 1.0].
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image


@dataclass
class OCRToken:
    """Single word or phrase token detected by PDF vector extraction or OCR in pixel space."""
    text: str
    x_min: int
    y_min: int
    x_max: int
    y_max: int
    confidence: float
    W: int
    H: int
    dir_vec: Tuple[float, float] = (1.0, 0.0)
    quad_points: Optional[List[Tuple[int, int]]] = None

    @property
    def is_rotated(self) -> bool:
        return abs(self.dir_vec[1]) > 0.12 or self.dir_vec[0] < 0.95

    @property
    def bbox(self) -> Tuple[int, int, int, int]:
        return (self.x_min, self.y_min, self.x_max, self.y_max)

    @property
    def width(self) -> int:
        return max(1, self.x_max - self.x_min)

    @property
    def height(self) -> int:
        return max(1, self.y_max - self.y_min)

    @property
    def center_x(self) -> float:
        return (self.x_min + self.x_max) / 2.0

    @property
    def center_y(self) -> float:
        return (self.y_min + self.y_max) / 2.0

    @property
    def norm_x(self) -> float:
        return self.center_x / max(1, self.W)

    @property
    def norm_y(self) -> float:
        return self.center_y / max(1, self.H)

    @property
    def norm_bbox(self) -> Tuple[float, float, float, float]:
        return (
            self.x_min / max(1, self.W),
            self.y_min / max(1, self.H),
            self.x_max / max(1, self.W),
            self.y_max / max(1, self.H),
        )


@dataclass
class OCRLine:
    """Logical phrase or line formed by PDF vector lines or grouped OCRTokens."""
    tokens: List[OCRToken] = field(default_factory=list)
    W: int = 1
    H: int = 1
    dir_vec: Tuple[float, float] = (1.0, 0.0)
    quad_points: Optional[List[Tuple[int, int]]] = None

    @property
    def is_rotated(self) -> bool:
        return abs(self.dir_vec[1]) > 0.12 or self.dir_vec[0] < 0.95

    @property
    def text(self) -> str:
        sorted_toks = sorted(self.tokens, key=lambda t: t.x_min)
        return " ".join(t.text for t in sorted_toks)

    @property
    def x_min(self) -> int:
        return min(t.x_min for t in self.tokens) if self.tokens else 0

    @property
    def y_min(self) -> int:
        return min(t.y_min for t in self.tokens) if self.tokens else 0

    @property
    def x_max(self) -> int:
        return max(t.x_max for t in self.tokens) if self.tokens else 0

    @property
    def y_max(self) -> int:
        return max(t.y_max for t in self.tokens) if self.tokens else 0

    @property
    def width(self) -> int:
        return max(1, self.x_max - self.x_min)

    @property
    def height(self) -> int:
        return max(1, self.y_max - self.y_min)

    @property
    def bbox(self) -> Tuple[int, int, int, int]:
        return (self.x_min, self.y_min, self.x_max, self.y_max)

    @property
    def center_x(self) -> float:
        return (self.x_min + self.x_max) / 2.0

    @property
    def center_y(self) -> float:
        return (self.y_min + self.y_max) / 2.0

    @property
    def norm_x(self) -> float:
        return self.center_x / max(1, self.W)

    @property
    def norm_y(self) -> float:
        return self.center_y / max(1, self.H)


def preprocess_for_ocr(
    image: Image.Image,
    scale_factor: float = 2.0,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Create an upscaled grayscale copy and an adaptive/binary thresholded copy
    strictly in memory without modifying the input PIL.Image.

    Returns:
        (upscaled_gray, upscaled_thresh)
    """
    # Convert a copy to RGB -> grayscale in memory
    rgb_arr = np.array(image.convert("RGB"), copy=True)
    gray = cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2GRAY)

    h, w = gray.shape[:2]
    new_w = int(round(w * scale_factor))
    new_h = int(round(h * scale_factor))

    upscaled_gray = cv2.resize(gray, (new_w, new_h), interpolation=cv2.INTER_CUBIC)

    # Combine Otsu/binary threshold with adaptive threshold so both crisp fonts
    # and shaded boxes (like shaded minimums or briefing strips) remain sharp.
    _, otsu_thresh = cv2.threshold(
        upscaled_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    adaptive_thresh = cv2.adaptiveThreshold(
        upscaled_gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        12,
    )
    # Bitwise OR / blend keeps text black where both or Otsu detects dark ink
    combined_thresh = cv2.bitwise_or(otsu_thresh, adaptive_thresh)
    return upscaled_gray, combined_thresh


def group_tokens_into_lines(
    tokens: List[OCRToken],
    W: int,
    H: int,
    max_y_diff_ratio: float = 0.011,
    max_x_gap_ratio: float = 0.032,
) -> List[OCRLine]:
    """
    Group nearby OCR word tokens on the same horizontal line into logical
    phrases/blocks before evaluating rules.

    Tokens on the same horizontal row whose horizontal gap is small (within the
    same column/cell) are merged into a single `OCRLine`.
    """
    if not tokens:
        return []

    sorted_tokens = sorted(tokens, key=lambda t: (t.center_y, t.x_min))
    max_y_px = max(6.0, H * max_y_diff_ratio)
    max_x_gap_px = max(14.0, W * max_x_gap_ratio)

    # First pass: bucket into horizontal rows by y-center proximity
    row_buckets: List[List[OCRToken]] = []
    for tok in sorted_tokens:
        placed = False
        for bucket in row_buckets:
            avg_y = sum(b.center_y for b in bucket) / len(bucket)
            avg_h = sum(b.height for b in bucket) / len(bucket)
            y_tol = max(max_y_px, avg_h * 0.65)
            if abs(tok.center_y - avg_y) <= y_tol:
                bucket.append(tok)
                placed = True
                break
        if not placed:
            row_buckets.append([tok])

    # Second pass: within each row bucket, sort left-to-right and split when
    # horizontal gap exceeds max_x_gap_px (e.g., separate briefing strip cells)
    lines: List[OCRLine] = []
    for bucket in row_buckets:
        bucket_sorted = sorted(bucket, key=lambda t: t.x_min)
        current_group: List[OCRToken] = [bucket_sorted[0]]
        for tok in bucket_sorted[1:]:
            prev = current_group[-1]
            gap = tok.x_min - prev.x_max
            dynamic_gap = max(max_x_gap_px, max(prev.height, tok.height) * 1.8)
            if gap <= dynamic_gap:
                current_group.append(tok)
            else:
                lines.append(OCRLine(tokens=current_group, W=W, H=H))
                current_group = [tok]
        if current_group:
            lines.append(OCRLine(tokens=current_group, W=W, H=H))

    return sorted(lines, key=lambda ln: (ln.y_min, ln.x_min))


class OCREngine:
    """
    Multi-backend OCR engine supporting `pytesseract`, `rapidocr-onnxruntime`,
    and `easyocr` with automatic fallback and 2x upscaled pre-processing.
    """

    def __init__(self, backend: str = "auto", scale_factor: float = 2.0):
        self.backend = backend.lower()
        self.scale_factor = scale_factor
        self._easyocr_reader = None
        self._rapidocr_reader = None

    def _resolve_backend(self) -> str:
        if self.backend in ("pytesseract", "tesseract"):
            return "pytesseract"
        if self.backend in ("rapidocr", "rapidocr-onnxruntime"):
            return "rapidocr"
        if self.backend == "easyocr":
            return "easyocr"

        # Auto-select highest-accuracy fast local ONNX/CPU backend available
        try:
            from rapidocr_onnxruntime import RapidOCR  # noqa: F401
            return "rapidocr"
        except ImportError:
            pass

        try:
            import pytesseract  # noqa: F401
            if shutil.which("tesseract") or shutil.which("/opt/homebrew/bin/tesseract"):
                return "pytesseract"
        except ImportError:
            pass

        try:
            import easyocr  # noqa: F401
            return "easyocr"
        except ImportError:
            pass

        return "pytesseract"

    def extract(
        self,
        image: Image.Image,
        vertical_lines_x: Optional[List[int]] = None,
    ) -> Tuple[List[OCRToken], List[OCRLine]]:
        """
        Run OCR on an in-memory 2x upscaled copy of `image`, map all bounding boxes
        back to the original `(W, H)` pixel space, and group tokens into `OCRLine`s.
        """
        W, H = image.size
        upscaled_gray, upscaled_thresh = preprocess_for_ocr(
            image, scale_factor=self.scale_factor
        )

        active_backend = self._resolve_backend()
        if active_backend == "pytesseract":
            tokens = self._extract_pytesseract(upscaled_gray, upscaled_thresh, W, H)
        elif active_backend == "rapidocr":
            tokens = self._extract_rapidocr(upscaled_gray, W, H)
        elif active_backend == "easyocr":
            tokens = self._extract_easyocr(upscaled_gray, W, H)
        else:
            tokens = self._extract_pytesseract(upscaled_gray, upscaled_thresh, W, H)

        lines = group_tokens_into_lines(tokens, W=W, H=H)
        return tokens, lines

    def _map_box_to_orig(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        W: int,
        H: int,
    ) -> Tuple[int, int, int, int]:
        inv = 1.0 / self.scale_factor
        ox1 = max(0, min(W - 1, int(round(x1 * inv))))
        oy1 = max(0, min(H - 1, int(round(y1 * inv))))
        ox2 = max(ox1 + 1, min(W, int(round(x2 * inv))))
        oy2 = max(oy1 + 1, min(H, int(round(y2 * inv))))
        return ox1, oy1, ox2, oy2

    def _extract_pytesseract(
        self,
        upscaled_gray: np.ndarray,
        upscaled_thresh: np.ndarray,
        W: int,
        H: int,
    ) -> List[OCRToken]:
        import pytesseract
        from pytesseract import Output

        tess_cmd = shutil.which("tesseract") or "/opt/homebrew/bin/tesseract"
        if shutil.which(tess_cmd):
            pytesseract.pytesseract.tesseract_cmd = tess_cmd

        tokens: List[OCRToken] = []
        # Run primary pass on upscaled thresholded image and secondary pass on
        # upscaled grayscale to capture any low-contrast or small text
        for img_input, psm in [(upscaled_thresh, "--oem 3 --psm 11"), (upscaled_gray, "--oem 3 --psm 6")]:
            data = pytesseract.image_to_data(img_input, config=psm, output_type=Output.DICT)
            n_boxes = len(data.get("text", []))
            for i in range(n_boxes):
                raw_text = (data["text"][i] or "").strip()
                if not raw_text:
                    continue
                conf_val = float(data["conf"][i]) if data["conf"][i] != "-1" else 0.0
                if conf_val < 15.0:
                    continue
                x = float(data["left"][i])
                y = float(data["top"][i])
                w = float(data["width"][i])
                h = float(data["height"][i])
                if w <= 2 or h <= 2:
                    continue
                # Ignore massive artifact boxes spanning >50% of image height
                if h > upscaled_gray.shape[0] * 0.25:
                    continue

                ox1, oy1, ox2, oy2 = self._map_box_to_orig(x, y, x + w, y + h, W, H)
                tokens.append(
                    OCRToken(
                        text=raw_text,
                        x_min=ox1,
                        y_min=oy1,
                        x_max=ox2,
                        y_max=oy2,
                        confidence=conf_val / 100.0,
                        W=W,
                        H=H,
                    )
                )

        return _deduplicate_tokens(tokens)

    def _extract_rapidocr(
        self,
        upscaled_gray: np.ndarray,
        W: int,
        H: int,
    ) -> List[OCRToken]:
        from rapidocr_onnxruntime import RapidOCR

        if self._rapidocr_reader is None:
            self._rapidocr_reader = RapidOCR()

        result, _ = self._rapidocr_reader(upscaled_gray)
        tokens: List[OCRToken] = []
        if not result:
            return tokens

        up_h, up_w = upscaled_gray.shape[:2]
        for item in result:
            box_pts, raw_text, conf = item[0], str(item[1]).strip(), float(item[2])
            if not raw_text:
                continue
            xs = [pt[0] for pt in box_pts]
            ys = [pt[1] for pt in box_pts]
            x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
            # Filter out tall vertical side-margin date stamps or huge tilted artifacts
            if (y2 - y1) > up_h * 0.065 or x1 < up_w * 0.038 or x2 > up_w * 0.962:
                continue
            # Split multi-word phrase boxes proportionally into word tokens so
            # both token-level and line-level rules work seamlessly
            words = raw_text.split()
            total_chars = max(1, sum(len(w) for w in words))
            cur_x = x1
            box_w = max(1.0, x2 - x1)
            for idx, word in enumerate(words):
                frac = len(word) / total_chars
                w_px = box_w * frac
                wx1 = cur_x
                wx2 = x2 if idx == len(words) - 1 else (cur_x + w_px)
                cur_x = wx2
                ox1, oy1, ox2, oy2 = self._map_box_to_orig(wx1, y1, wx2, y2, W, H)
                tokens.append(
                    OCRToken(
                        text=word,
                        x_min=ox1,
                        y_min=oy1,
                        x_max=ox2,
                        y_max=oy2,
                        confidence=conf,
                        W=W,
                        H=H,
                    )
                )
        return _deduplicate_tokens(tokens)

    def _extract_easyocr(
        self,
        upscaled_gray: np.ndarray,
        W: int,
        H: int,
    ) -> List[OCRToken]:
        import easyocr

        if self._easyocr_reader is None:
            self._easyocr_reader = easyocr.Reader(["en"], gpu=False, verbose=False)

        results = self._easyocr_reader.readtext(upscaled_gray)
        tokens: List[OCRToken] = []
        up_h, up_w = upscaled_gray.shape[:2]
        for box_pts, raw_text, conf in results:
            raw_text = str(raw_text).strip()
            if not raw_text:
                continue
            xs = [pt[0] for pt in box_pts]
            ys = [pt[1] for pt in box_pts]
            x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
            if (y2 - y1) > up_h * 0.065 or x1 < up_w * 0.038 or x2 > up_w * 0.962:
                continue
            words = raw_text.split()
            total_chars = max(1, sum(len(w) for w in words))
            cur_x = x1
            box_w = max(1.0, x2 - x1)
            for idx, word in enumerate(words):
                frac = len(word) / total_chars
                w_px = box_w * frac
                wx1 = cur_x
                wx2 = x2 if idx == len(words) - 1 else (cur_x + w_px)
                cur_x = wx2
                ox1, oy1, ox2, oy2 = self._map_box_to_orig(wx1, y1, wx2, y2, W, H)
                tokens.append(
                    OCRToken(
                        text=word,
                        x_min=ox1,
                        y_min=oy1,
                        x_max=ox2,
                        y_max=oy2,
                        confidence=float(conf),
                        W=W,
                        H=H,
                    )
                )
        return _deduplicate_tokens(tokens)


def _iou(a: OCRToken, b: OCRToken) -> float:
    ix1 = max(a.x_min, b.x_min)
    iy1 = max(a.y_min, b.y_min)
    ix2 = min(a.x_max, b.x_max)
    iy2 = min(a.y_max, b.y_max)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = float((ix2 - ix1) * (iy2 - iy1))
    area_a = float(a.width * a.height)
    area_b = float(b.width * b.height)
    return inter / max(1.0, area_a + area_b - inter)


def _deduplicate_tokens(tokens: List[OCRToken]) -> List[OCRToken]:
    """Remove duplicate tokens from multi-pass OCR by keeping higher confidence."""
    sorted_toks = sorted(tokens, key=lambda t: t.confidence, reverse=True)
    kept: List[OCRToken] = []
    for tok in sorted_toks:
        duplicate = False
        for k in kept:
            if _iou(tok, k) > 0.45 and (
                tok.text.upper() == k.text.upper()
                or tok.text.upper() in k.text.upper()
                or k.text.upper() in tok.text.upper()
            ):
                duplicate = True
                break
        if not duplicate:
            kept.append(tok)
    return sorted(kept, key=lambda t: (t.y_min, t.x_min))
