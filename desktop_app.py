"""
Native macOS Desktop App for FAA Approach Plate 4-Color Tactical Enhancer (`desktop_app.py`).

Runs purely on the local desktop via macOS WebKit (`pywebview`) without Streamlit
or an external web browser. Provides an uncluttered, modern cockpit EFB interface.
"""

from __future__ import annotations

import sys
from pathlib import Path

import webview

from desktop_api import DesktopAPI


def main() -> None:
    project_root = Path(__file__).resolve().parent
    ui_html = project_root / "desktop_ui" / "index.html"

    if not ui_html.is_file():
        print(f"[!] Error: Could not locate desktop UI at {ui_html}", file=sys.stderr)
        sys.exit(1)

    api = DesktopAPI()

    window = webview.create_window(
        title="FAA Approach Plate 4-Color Tactical Enhancer",
        url=str(ui_html),
        js_api=api,
        width=1440,
        height=940,
        min_size=(1080, 720),
        text_select=True,
        zoomable=True,
        confirm_close=False,
    )

    api.set_window(window)

    # Bring native window to foreground on macOS
    try:
        from AppKit import NSApplication

        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass

    webview.start(debug=False)


if __name__ == "__main__":
    main()
