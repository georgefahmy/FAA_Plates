"""
Markdown "5 A's" Pilot Tactical Briefing Generator (`enhancer/briefing.py`).

Generates a structured, cockpit-ready markdown briefing summary from the
deterministically extracted OCR metadata and 4-color highlight rules.
"""

from __future__ import annotations

from typing import List

from enhancer.rules import ExtractedPlateMetadata, HighlightBox


def generate_5a_briefing(
    meta: ExtractedPlateMetadata,
    highlights: List[HighlightBox],
    aircraft_categories: List[str],
    target_approaches: List[str],
) -> str:
    """
    Generate a structured "5 A's" (ATIS/Weather, Avionics/Course, Altitudes/Minimums,
    Airspeed/Missed Approach, Alerts/Gotchas) tactical briefing in Markdown.
    """
    cats_str = ", ".join(aircraft_categories) if aircraft_categories else "A, B"
    apch_str = ", ".join(target_approaches) if target_approaches else "LPV, LNAV/VNAV"

    atis_str = (
        ", ".join(meta.atis_awos_freqs)
        if meta.atis_awos_freqs
        else "Verify on chart comms strip"
    )
    apch_con_str = (
        ", ".join(meta.approach_freqs)
        if meta.approach_freqs
        else "Verify Approach Control frequency"
    )
    twr_str = (
        ", ".join(meta.tower_ctaf_freqs)
        if meta.tower_ctaf_freqs
        else "Verify Tower / CTAF"
    )
    gnd_str = (
        ", ".join(meta.gnd_clnc_freqs)
        if meta.gnd_clnc_freqs
        else "N/A or verify Ground/Clearance"
    )

    crs_str = f"{meta.app_crs}°" if meta.app_crs else "Verify in Briefing Strip"
    if_str = meta.if_name or "Verify IF"
    faf_str = meta.faf_name or "Verify FAF"
    wps_str = ", ".join(meta.waypoints) if meta.waypoints else "None extracted"

    prof_alts = (
        " → ".join(f"{a} ft" for a in meta.profile_altitudes)
        if meta.profile_altitudes
        else "Verify profile stepdown altitudes"
    )

    min_lines_md: List[str] = []
    for k, v in meta.minimums_rows.items():
        marker = "**[TARGET]** " if k in [a.upper() for a in target_approaches] else ""
        min_lines_md.append(f"  - {marker}**{k}**: `{v}`")
    minimums_block = (
        "\n".join(min_lines_md)
        if min_lines_md
        else f"  - Target ({apch_str} / Cat {cats_str}): Verify in bottom-left minimums table"
    )

    missed_txt = (
        " ".join(meta.missed_approach_text)
        if meta.missed_approach_text
        else "Verify top-right Missed Approach text box"
    )
    hold_fix_str = meta.holding_fix or "Published Missed Approach Holding Fix"

    gp_str = (
        f"{meta.glidepath_angle:.2f}°"
        + (" ⚠️ **STEEP (> 3.00°)**" if meta.glidepath_angle > 3.00 else " (Standard)")
        if meta.glidepath_angle is not None
        else "Verify GP in Profile View"
    )
    tch_str = f"{meta.tch} ft" if meta.tch else "Verify TCH"
    msa_str = (
        ", ".join(f"{m} ft" for m in meta.msa_altitudes)
        if meta.msa_altitudes
        else "Verify MSA Circle (25 NM)"
    )
    obstacles_str = (
        ", ".join(f"{o} ft" for o in meta.high_obstacles[:8])
        if meta.high_obstacles
        else "None flagged"
    )

    restrictions_md = (
        "\n".join(f"  - 🔴 `{r}`" for r in dict.fromkeys(meta.red_restrictions))
        if meta.red_restrictions
        else "  - Standard notes — review T/A box carefully"
    )

    counts = {
        "CYAN": sum(1 for h in highlights if h.color_key == "CYAN"),
        "MAGENTA": sum(1 for h in highlights if h.color_key == "MAGENTA"),
        "AMBER": sum(1 for h in highlights if h.color_key == "AMBER"),
        "RED": sum(1 for h in highlights if h.color_key == "RED"),
    }

    return f"""### Tactical "5 A's" Approach Briefing — {meta.title}

1. **🟦 1st A — ATIS & Communications (CYAN Layer — `{counts['CYAN']}` highlights)**
   - **Weather (ATIS / AWOS / ASOS):** {atis_str}
   - **Approach Control:** {apch_con_str}
   - **Tower / CTAF / UNICOM:** {twr_str}
   - **Ground / Clearance:** {gnd_str}

2. **🟪 2nd A — Avionics, Course & Fixes (MAGENTA Layer — `{counts['MAGENTA']}` highlights)**
   - **Primary Approach Course (`APP CRS`):** **{crs_str}**
   - **Initial / Final Approach Fixes:** IF `{if_str}` → FAF `{faf_str}`
   - **Active Flight Path Waypoints:** {wps_str}

3. **🟪 3rd A — Altitudes & Target Minimums (MAGENTA Layer — Cat `{cats_str}`)**
   - **Profile Crossing Altitudes:** {prof_alts}
   - **Minimums Table ({apch_str}):**
{minimums_block}

4. **🟧 4th A — Airspeed, Escape & Missed Approach (AMBER Layer — `{counts['AMBER']}` highlights)**
   - **Missed Approach Instructions:** {missed_txt}
   - **Missed Approach Holding Fix:** **{hold_fix_str}**

5. **🟥 5th A — Alerts, Gotchas & Terrain Hazards (RED Layer — `{counts['RED']}` highlights)**
   - **Glidepath Angle & TCH:** **GP {gp_str}** | **TCH {tch_str}**
   - **Minimum Safe Altitude (MSA 25 NM):** **{msa_str}**
   - **Plan View Spot Elevations / Obstacles:** {obstacles_str}
   - **Procedure Restrictions / Climb Gradients / Notes:**
{restrictions_md}
"""
