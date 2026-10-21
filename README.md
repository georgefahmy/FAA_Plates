# Deterministic FAA Approach Plate PDF & PNG Enhancer (`faa-png-plate-enhancer`)

A safety-critical Python CLI and Streamlit Web application built around official FAA Digital Terminal Procedures Publication (**d-TPP**) vector **PDFs** (`d-TPP_Metafile.xml` + `DDTPPA..E`) as well as raster images (`.png`, `.jpg`, `.jpeg`), rendering a standardized **"4-Color Tactical Briefing"** RGBA highlight overlay.

---

## 🔒 Safety-Critical Non-Destructive Guarantee

- **Zero Generative AI / Zero Pixel or Vector Mutation:** Underlying FAA PDF vectors, text glyphs, and rendered pixels are **never** passed through generative models, image-to-image translation, or text re-rendering.
- **Native PDF Vector Extraction (`enhancer/pdf_engine.py`):** Extracts exact 100%-accurate vector text spans, rotated 4-corner text quadrilaterals (`dir_vec` + `recover_line_quad`), bold approach course/slope vectors (`lw=1.44`), curved dashed missed-approach escape polylines (`lw=0.72`), holding racetracks, and 16-Bezier MSA circles directly from the FAA PDF via `PyMuPDF` (`pymupdf`).
- **Pure RGBA Alpha-Compositing & Non-Destructive PDF Export (`enhancer/compositor.py`):** Every highlight (rounded rectangles, rotated quadrilaterals, and 2x-supersampled uniform-alpha flight-path ribbons) is drawn onto an isolated transparent layer (`PIL.Image.new("RGBA", size, (0, 0, 0, 0))`) and composited strictly on top of the locked base chart, or inserted as an overlay layer (`overlay=True`) onto the original vector PDF.
- **Strict Base Pixel Integrity Verification:** The pipeline cryptographically verifies that all underlying chart pixels outside highlight regions are 100% bit-for-bit identical to the official FAA source.

---

## 🐍 Virtual Environment & Prerequisites

This project is configured and tested with Python 3.14 using the user's dedicated `plates` virtual environment:

```bash
# Interpreter path:
~/.virtualenvs/plates/bin/python

# Core dependencies installed:
# - pymupdf (1.28.2)
# - opencv-python-headless (5.0.0.93)
# - pillow (12.3.0)
# - numpy (2.5.4)
# - streamlit (1.66.0)
```

To install or verify requirements:
```bash
~/.virtualenvs/plates/bin/pip install -r requirements.txt
```

---

## 🎨 The "4-Color Tactical Briefing" Palette & Heuristics

| Color | RGBA Tuple | Tactical Phase | Deterministic Rules (`enhancer/rules.py`) |
| :--- | :--- | :--- | :--- |
| **🟦 CYAN** | `(0, 215, 245, 80)` | **Communications & Setup** | Spatial zone `0.10*H < y < 0.22*H`. Matches `AWOS`, `ASOS`, `ATIS`, `APP CON`, `TOWER`, `CTAF`, `UNICOM`, `GND CON`, `CLNC` and VHF (`\b1[1-3]\d\.\d{1,3}\b`) / UHF (`\b[2-3]\d{2}\.\d{1,3}\b`) frequencies, grouping each facility header with the frequency immediately below it. |
| **🟪 MAGENTA** | `(240, 50, 215, 75)` | **Primary Flight Path, Fixes & Minimums** | 1. Top-left `APP CRS` (`y < 0.12*H, x < 0.30*W`) + 3-digit course (`\b\d{3}°?\b`).<br>2. Plan View (`0.20*H < y < 0.65*H`): Angled flight-path corridor along the bold (`lw=1.44`) final approach course vector, rotated quads for `app_crs` and fix altitudes, `(FAF)`, `(IF)`, and 5-letter waypoints (`\b[A-Z]{5}\b`).<br>3. Profile View: Angled descent-slope corridor along the bold (`lw=1.44`) profile descent vector, rotated `app_crs` quad, and 4-digit crossing altitudes (`\b[1-9]\d{2}0\b`).<br>4. Minimums Table: Full horizontal row from left border to selected Aircraft Category (`A`, `B`, `C`, `D`) for selected approaches (`LPV`, `LNAV/VNAV`, `LNAV`, `CIRCLING`, `ILS`, `LOC`). |
| **🟧 AMBER** | `(255, 165, 0, 85)` | **Missed Approach & Escape Action** | 1. Top-right `MISSED APPROACH:` box (`0.08*H < y < 0.18*H, x > 0.50*W`) excluding `*` / climb-gradient lines.<br>2. Plan View & Profile View dashed (`lw=0.72`) curved missed-approach escape polylines & holding pattern racetracks.<br>3. Profile View top-left procedural missed approach icon boxes.<br>4. Holding fix extracted from `direct <FIX> and hold` (`MISSED APCH FIX` and `<FIX>` in Plan & Profile Views). |
| **🟥 RED** | `(245, 50, 50, 80)` | **"Gotchas," Restrictions, Terrain & Hazards** | 1. Briefing Strip Notes (`0.08*H < y < 0.18*H, x < 0.58*W`) matching `Circling NA`, `Procedure NA`, `visibility reduction`, `Baro-VNAV`, `uncompensated`, `below`, `helicopter`, or `night`.<br>2. Climb gradient lines (`*` / `minimum climb` / `feet per NM`) in the Missed Approach box.<br>3. Profile View `GP` / `TCH` box with a bold `2px` Red border if `GP > 3.00°` (e.g., `4.00°` or `3.50°`).<br>4. Plan View `Procedure NA for arrival`, exact 16-Bezier `MSA` 25 NM circle + inner 4-digit altitude box, and 4-digit spot elevations/obstacles (`\b[2-9]\d{3}\b`). |

---

## ⚡ Streamlit Web Application (`app.py`)

Launch the interactive app:
```bash
~/.virtualenvs/plates/bin/streamlit run app.py
```

The Web UI features two distinct operation modes:

### 1. 🔍 Single Plate Tactical Viewer & Briefing
- **Built-in FAA d-TPP XML Catalog Browser:** Filter through 24,075+ charts by State, City, Airport (FAA LID / ICAO), and Chart Type (`IAP`, `APD`, `DP`, `STAR`, `MIN`).
- **Interactive Layer Toggles:** Enable or disable Cyan, Magenta, Amber, or Red layers in real time.
- **Aircraft Minimums Customizer:** Select Aircraft Category (`A`, `B`, `C`, `D`) and target approaches (`LPV`, `LNAV/VNAV`, `ILS`, `LOC`, `CIRCLING`).
- **Interactive Side-by-Side Comparison:** Instant high-DPI inspection of original vs. enhanced plate.
- **Pilot Briefing & Downloads:** Generates Markdown "5 A's" briefing summary and one-click downloads for enhanced vector `.pdf` and high-res `.png`.

### 2. ⚡ Batch Directory Enhancer (IAC Plates Only)
- **Directory Path Input:** Point to any folder (e.g. `/Users/GFahmy/Documents/RV-7/Airmate/ChartData`).
- **Targeted Discovery:** Automatically reads `Plates.sqlite` (or `d-TPP_Metafile.xml`) to identify all **11,151** Instrument Approach Procedure (`IAC` / `IAP` / `DAU`) charts.
- **Strict Non-IAC Protection:** All non-IAC charts (`APD` Airport Diagrams, `DP` Departures, `STAR` Arrivals, `MIN` Minimums) are strictly preserved and left 100% untouched.
- **Output File Format Selector:**
  - `PDF (Enhanced Vector PDF with Overlay)`
  - `PNG (High-Resolution RGBA Image)`
  - `Both (PDF + PNG)`
- **Output Destination:** Safe in-place overwrite (with atomic file renaming) or export to a dedicated subfolder.
- **Multithreaded Processing:** Configurable worker threads (1–16) with live progress bar, speed (plates/sec), elapsed time, ETA, and recent file activity log.

---

## 💻 Command-Line Interface (`cli.py`)

### 1. Single Plate Enhancement
```bash
# Enhance a single FAA vector PDF to PNG:
~/.virtualenvs/plates/bin/python cli.py /Users/GFahmy/Documents/RV-7/Airmate/FAA_pdf_data/DDTPPD_261001/09260R32.PDF \
  -o e16_rnav32_enhanced.png \
  --categories A B --approaches LPV LNAV/VNAV

# Enhance directly to vector PDF:
~/.virtualenvs/plates/bin/python cli.py /Users/GFahmy/Documents/RV-7/Airmate/FAA_pdf_data/DDTPPD_261001/09260R32.PDF \
  -o e16_rnav32_enhanced.pdf

# Select airport from catalog and output pilot briefing:
~/.virtualenvs/plates/bin/python cli.py --airport E16 --chart "RNAV (GPS) RWY 32" \
  -o e16_rnav32.pdf --briefing e16_briefing.md
```

### 2. Batch Directory Enhancement (IAC Only)
```bash
# Safe in-place overwrite of all 11,151 IAC plates as vector PDFs (8 workers):
~/.virtualenvs/plates/bin/python cli.py \
  --batch-dir /Users/GFahmy/Documents/RV-7/Airmate/ChartData \
  --format PDF \
  --workers 8

# Batch enhance to high-res PNGs in a separate output directory:
~/.virtualenvs/plates/bin/python cli.py \
  --batch-dir /Users/GFahmy/Documents/RV-7/Airmate/ChartData \
  --batch-output-dir /Users/GFahmy/Documents/RV-7/Airmate/ChartData/Enhanced_IAC_PNG \
  --format PNG \
  --workers 8

# Test run on the first 10 plates:
~/.virtualenvs/plates/bin/python cli.py \
  --batch-dir /Users/GFahmy/Documents/RV-7/Airmate/ChartData \
  --format PDF \
  --batch-limit 10
```

---

## 📦 Standalone Airmate / Dynon Avionics ChartData Recreator (`recreate_airmate_chartdata.py`)

Rebuilds an entire Airmate and **Dynon Avionics SkyView** compatible `ChartData` structure (SQLite database + single-page PNG or PDF folder) **100% standalone** from official FAA source data (`d-TPP_Metafile.xml` + PDF volumes `DDTPPA`..`E`), with zero dependency on proprietary reference data.

### 1. Dynon Avionics Compatible PNG Generation (Default)

Dynon SkyView displays georeferenced approach plates and airport diagrams as raster PNG images. You can configure whether to generate **Enhanced PNGs** (with 4-color tactical briefings on IAC approach plates) or **Non-Enhanced PNGs**:

```bash
# Generate Dynon-compatible folder with Enhanced PNGs (4-color briefing for IAC):
~/.virtualenvs/plates/bin/python recreate_airmate_chartdata.py \
  --output-dir /Users/GFahmy/Documents/RV-7/Airmate/ChartData \
  --format png --enhanced --workers 8

# Generate Dynon-compatible folder with Non-Enhanced / Standard clean PNGs:
~/.virtualenvs/plates/bin/python recreate_airmate_chartdata.py \
  --output-dir /Users/GFahmy/Documents/RV-7/Airmate/ChartData \
  --format png --non-enhanced --workers 8

# Quick validation test on first 20 plates:
~/.virtualenvs/plates/bin/python recreate_airmate_chartdata.py \
  --output-dir /tmp/test_chartdata --format png --limit 20
```

### 2. FAA Vector PDF Generation

```bash
# Generate single-page vector PDFs:
~/.virtualenvs/plates/bin/python recreate_airmate_chartdata.py \
  --output-dir /Users/GFahmy/Documents/RV-7/Airmate/ChartData \
  --format pdf
```

### Key Output Features:
- **Dynon SkyView Raster Compatibility:** Output format defaults to `.png` with configurable DPI (default: 200 DPI, giving ~1076x1650 px resolution matching SkyView display standards).
- **Tactical Briefing Option:** When `--enhanced` is active, all IAC/IAP approach plates are rendered with non-destructive 4-color tactical briefing overlays, while APD diagrams, DP departures, STARs, and minimums are rendered as clean, crisp standard PNGs.
- **Strict Single-Page Splitting:** Multi-page FAA booklets (Takeoff Minimums, Alternate Minimums, etc.) are automatically split into individual numbered single-page charts (`EC1ALT01.png`, `EC1ALT02.png`, etc.) and indexed by vector text matching.
- **Relational `Plates.sqlite`:** Creates SQLite tables (`Airports`, `Charts`, `Countries`, `Regions`) mapping 5,294 airports and 30,145 charts, with `Charts.File_Name` automatically aligned with disk extensions (`.png` or `.PDF`).
- **Standardized Folder Layout:** Produces `Plates/Plates.sqlite` and `Plates/US/` containing all plates ready for USB transfer to Dynon Avionics SkyView or mobile EFBs.

---

## 🧪 Running Tests

Run the unit and integration tests using Python's standard `unittest` framework:

```bash
~/.virtualenvs/plates/bin/python -m unittest tests/test_batch_engine.py
```
