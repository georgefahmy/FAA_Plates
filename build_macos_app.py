"""
Builds the native macOS 'FAA Plate Enhancer.app' standalone application bundle.
"""

import shutil
import subprocess
import sys
from pathlib import Path


def build_app_bundle(project_dir: Path) -> Path:
    """Build native standalone Mach-O macOS .app bundle using PyInstaller."""
    pyinstaller_bin = (
        Path(sys.executable).parent / "pyinstaller"
        if (Path(sys.executable).parent / "pyinstaller").is_file()
        else Path("pyinstaller")
    )
    icon_path = project_dir / "desktop_ui" / "assets" / "app_icon.png"

    cmd = [
        str(pyinstaller_bin),
        "--name",
        "FAA Plate Enhancer",
        "--windowed",
        "--noconfirm",
        "--clean",
        "--osx-bundle-identifier",
        "com.antigravity.faaplateenhancer",
        "--icon",
        str(icon_path),
        "--add-data",
        "desktop_ui:desktop_ui",
        "--collect-all",
        "webview",
        "--hidden-import",
        "PIL",
        "--hidden-import",
        "pymupdf",
        str(project_dir / "desktop_app.py"),
    ]

    print("[*] Compiling native Mach-O standalone macOS application bundle...")
    subprocess.run(cmd, cwd=project_dir, check=True)

    dist_app = project_dir / "dist" / "FAA Plate Enhancer.app"
    local_app = project_dir / "FAA Plate Enhancer.app"

    if local_app.exists():
        shutil.rmtree(local_app)
    shutil.copytree(dist_app, local_app)

    apps_dir = Path("/Applications/FAA Plate Enhancer.app")
    try:
        if apps_dir.exists():
            shutil.rmtree(apps_dir)
        shutil.copytree(dist_app, apps_dir)
        print(f"[+] Installed to macOS Applications: {apps_dir}")
    except Exception as e:
        print(f"[!] Note: Could not copy to /Applications automatically ({e})")

    print(f"[+] Native standalone app ready: {local_app}")
    return local_app


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    build_app_bundle(root)
