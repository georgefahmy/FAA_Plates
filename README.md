# Deterministic FAA Approach Plate PDF & PNG Enhancer (`faa-png-plate-enhancer`)

A safety-critical Python CLI and Streamlit Web application built around official FAA Digital Terminal Procedures Publication (**d-TPP**) vector **PDFs** (`d-TPP_Metafile.xml` + `DDTPPA..E`) as well as raster images (`.png`, `.jpg`, `.jpeg`), rendering a standardized **"4-Color Tactical Briefing"** RGBA highlight overlay.

## 🔒 Safety-Critical Non-Destructive Guarantee

- **Zero Generative AI / Zero Pixel or Vector Mutation:** The underlying FAA PDF vectors and rendered pixels are **never** passed through generative image models, image-to-image translation, or text re-rendering.
- **Native PDF Vector Extraction (`enhancer/pdf_engine.py`):** Extracts exact 100%-accurate vector text spans, rotated 4-corner text quadrilaterals (`dir_vec` + `recover_line_quad`), bold approach course/slope vectors (`lw=1.44`), curved dashed missed-approach escape polylines (`lw=0.72`), holding racetracks, and 16-Bezier MSA circles directly from the FAA PDF via `PyMuPDF` (`fitz`).
- **Pure RGBA Alpha-Compositing & Non-Destructive PDF Export (`enhancer/compositor.py`):** Every highlight (rounded rectangles, rotated quadrilaterals, and 2x-supersampled uniform-alpha flight-path ribbons) is drawn onto a separate transparent layer (`PIL.Image.new("RGBA", size, (0, 0, 0, 0))`) and composited strictly on top of the locked base chart via `Image.alpha_composite()`, or inserted as an overlay layer (`overlay=True`) onto the original vector PDF.

---

## 🗂️ Built-In FAA d-TPP XML & PDF Plate Browser (`enhancer/plate_browser.py`)

Automatically parses `/Users/GFahmy/Documents/RV-7/Airmate/FAA_pdf_data/DDTPPE_261001/d-TPP_Metafile.xml` and indexes **24,075+ official FAA PDFs** across `DDTPPA_261001` through `DDTPPE_261001` into a fast local SQLite cache (`.cache/dtpp_catalog.sqlite`), allowing instant search and filtering by:
- **State** (54 US states & territories)
- **City**
- **Airport** (FAA LID, ICAO ID, or Airport Name, e.g., `E16`, `KSMO`, `KSJC`)
- **Chart Type** (`IAP`, `APD`, `DP`, `STAR`, `MIN`, `ODP`, `HOT`)
- **Direct Plate Search** (e.g., `E16 RNAV 32` or `KSMO RNAV 21`)

---

## 🎨 The "4-Color Tactical Briefing" Palette & Heuristics

| Color | RGBA Tuple | Tactical Phase | Deterministic Rules (`enhancer/rules.py`) |
| :--- | :--- | :--- | :--- |
| **🟦 CYAN** | `(0, 215, 245, 80)` | **Communications & Setup** | Spatial zone `0.10*H < y < 0.22*H`. Matches `AWOS`, `ASOS`, `ATIS`, `APP CON`, `TOWER`, `CTAF`, `UNICOM`, `GND CON`, `CLNC` and VHF (`\b1[1-3]\d\.\d{1,3}\b`) / UHF (`\b[2-3]\d{2}\.\d{1,3}\b`) frequencies, grouping each facility header with the frequency immediately below it. |
| **🟪 MAGENTA** | `(240, 50, 215, 75)` | **Primary Flight Path, Fixes & Minimums** | 1. Top-left `APP CRS` (`y < 0.12*H, x < 0.30*W`) + 3-digit course (`\b\d{3}°?\b`).<br>2. Plan View (`0.20*H < y < 0.65*H`): Angled flight-path corridor along the bold (`lw=1.44`) final approach course vector, rotated quads for `app_crs` and fix altitudes, `(FAF)`, `(IF)`, and 5-letter waypoints (`\b[A-Z]{5}\b`).<br>3. Profile View: Angled descent-slope corridor along the bold (`lw=1.44`) profile descent vector, rotated `app_crs` quad, and 4-digit crossing altitudes (`\b[1-9]\d{2}0\b`).<br>4. Minimums Table: Full horizontal row from left border to selected Aircraft Category (`A`, `B`, `C`, `D`) for selected approaches (`LPV`, `LNAV/VNAV`, `LNAV`, `CIRCLING`, `ILS`, `LOC`). |
| **🟧 AMBER** | `(255, 165, 0, 85)` | **Missed Approach & Escape Action** | 1. Top-right `MISSED APPROACH:` box (`0.08*H < y < 0.18*H, x > 0.50*W`) excluding `*` / climb-gradient lines.<br>2. Plan View & Profile View dashed (`lw=0.72`) curved missed-approach escape polylines & holding pattern racetracks.<br>3. Profile View top-left procedural missed approach icon boxes.<br>4. Holding fix extracted from `direct <FIX> and hold` (`MISSED APCH FIX` and `<FIX>` in Plan & Profile Views). |
| **🟥 RED** | `(245, 50, 50, 80)` | **"Gotchas," Restrictions, Terrain & Hazards** | 1. Briefing Strip Notes (`0.08*H < y < 0.18*H, x < 0.58*W`) matching `Circling NA`, `Procedure NA`, `visibility reduction`, `Baro-VNAV`, `uncompensated`, `below`, `helicopter`, or `night`.<br>2. Climb gradient lines (`*` / `minimum climb` / `feet per NM`) in the Missed Approach box.<br>3. Profile View `GP` / `TCH` box with a bold `2px` Red border if `GP > 3.00°` (e.g., `4.00°` or `3.50°`).<br>4. Plan View `Procedure NA for arrival`, exact 16-Bezier `MSA` 25 NM circle + inner 4-digit altitude box, and 4-digit spot elevations/obstacles (`\b[2-9]\d{3}\b`). |

---

## 🚀 Quickstart

### 1. Command-Line Interface (`cli.py`)

Enhance an official FAA d-TPP PDF directly (outputs both PNG and optional enhanced vector PDF):
```bash
~/.virtualenvs/tracker/bin/python3 cli.py /Users/GFahmy/Documents/RV-7/Airmate/FAA_pdf_data/DDTPPD_261001/09260R32.PDF \
  -o e16_rnav32_enhanced.png \
  --pdf-out e16_rnav32_enhanced.pdf \
  --categories A B --approaches LPV LNAV/VNAV
```

### 2. Batch Directory Enhancer (IAC Plates Only) via CLI

Scan any directory (such as an Airmate `ChartData` folder) and automatically batch-enhance **only** official Instrument Approach Procedure (`IAC` / `IAP`) vector PDF charts while leaving all other charts (`APD`, `DP`, `STAR`, `MIN`) 100% untouched:

```bash
# In-place safe enhancement of all IAC plates as vector PDFs (using 8 worker threads)
~/.virtualenvs/plates/bin/python cli.py --batch-dir /Users/GFahmy/Documents/RV-7/Airmate/ChartData --format PDF --workers 8

# Batch enhance to high-resolution PNG images in a separate folder
~/.virtualenvs/plates/bin/python cli.py --batch-dir /Users/GFahmy/Documents/RV-7/Airmate/ChartData \
  --batch-output-dir /Users/GFahmy/Documents/RV-7/Airmate/ChartData/Enhanced_IAC_PNG \
  --format PNG --batch-limit 10
```

### 3. Streamlit Web UI (`app.py`)

Launch the interactive app:
```bash
~/.virtualenvs/plates/bin/streamlit run app.py
```

Features in the Web UI:
- **🔍 Single Plate Tactical Viewer & Briefing:**
  - Built-in FAA d-TPP XML Catalog Browser (filter by State, City, Airport LID/ICAO, Chart Type).
  - Side-by-side original vs. 4-color tactical overlay comparison.
  - Interactive layer toggles (Cyan, Magenta, Amber, Red).
  - Aircraft Category & Minimums selection (A, B, C, D; LPV, LNAV/VNAV, etc.).
  - Instant Enhanced PNG and Vector PDF downloads + Markdown "5 A's" Pilot Briefing.
- **⚡ Batch Directory Enhancer (IAC Plates Only):**
  - Pass an extracted plates directory (e.g. `/Users/GFahmy/Documents/RV-7/Airmate/ChartData`).
  - Auto-discovers all 11,151+ IAC plates using `Plates.sqlite` (or `d-TPP_Metafile.xml`).
  - Safe in-place overwrite or separate export folder.
  - Multi-threaded processing with live progress bar, speed (plates/sec), elapsed time, ETA, and recent activity log.
  - Strict exclusion of non-IAC files (APD, DP, STAR, MIN untouched).

