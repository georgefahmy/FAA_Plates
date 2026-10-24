"""
Native macOS Desktop Applet for FAA Approach Plate 4-Color Tactical Enhancer (`desktop_app.py`).

Runs the Streamlit server locally in the background and renders it inside a native
macOS WebKit window via `pywebview`. Eliminates external web browsers, URL bars,
and browser tabs for a clean desktop app experience.
"""

from __future__ import annotations

import atexit
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import webview


def find_free_port(start_port: int = 8501) -> int:
    """Find the first available TCP port on localhost starting at start_port."""
    port = start_port
    while port < 65535:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1
    return start_port


def wait_for_server(port: int, timeout: float = 25.0) -> bool:
    """Poll Streamlit health check until the local server is responsive."""
    url = f"http://127.0.0.1:{port}/_stcore/health"
    start_t = time.time()
    while time.time() - start_t < timeout:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "FAAPlateDesktop/1.0"},
            )
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def main() -> None:
    project_root = Path(__file__).resolve().parent
    app_py = project_root / "app.py"

    if not app_py.is_file():
        print(f"[!] Error: Could not locate app.py at {app_py}", file=sys.stderr)
        sys.exit(1)

    port = find_free_port(8501)
    python_bin = sys.executable

    log_dir = project_root / ".cache"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / "desktop_streamlit.log", "w", encoding="utf-8")

    cmd = [
        python_bin,
        "-m",
        "streamlit",
        "run",
        str(app_py),
        f"--server.port={port}",
        "--server.address=127.0.0.1",
        "--server.headless=true",
        "--browser.serverAddress=127.0.0.1",
        "--browser.gatherUsageStats=false",
        "--global.developmentMode=false",
        "--client.toolbarMode=minimal",
    ]

    print(f"[*] Starting local background server on http://127.0.0.1:{port} ...")
    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=str(project_root),
        env=os.environ.copy(),
    )

    def cleanup() -> None:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            log_file.close()
        except Exception:
            pass

    atexit.register(cleanup)
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    if not wait_for_server(port, timeout=25.0):
        print(
            f"[!] Error: Timed out waiting for local Streamlit server to start on port {port}.",
            file=sys.stderr,
        )
        cleanup()
        sys.exit(1)

    print("[+] Server ready. Opening native macOS app window...")

    window = webview.create_window(
        title="Approach Plate 4-Color Tactical Enhancer",
        url=f"http://127.0.0.1:{port}",
        width=1440,
        height=940,
        min_size=(1080, 720),
        text_select=True,
        zoomable=True,
        confirm_close=False,
    )

    window.events.closed += cleanup

    # Bring native window to foreground on macOS
    try:
        from AppKit import NSApplication

        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass

    webview.start(debug=False)


if __name__ == "__main__":
    main()
