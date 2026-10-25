"""
Builds the native macOS 'FAA Plate Enhancer.app' application bundle.
"""

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from PIL import Image, ImageDraw


def generate_app_icon(output_icns_path: Path) -> None:
    """Generate high-resolution tactical aviation ICNS icon."""
    custom_icon_path = Path(__file__).resolve().parent / "desktop_ui" / "assets" / "app_icon.png"
    if custom_icon_path.is_file():
        base_img = Image.open(custom_icon_path).convert("RGBA")
    else:
        base_img = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
        d = ImageDraw.Draw(base_img)

        # Dark slate/navy rounded squircle background
        d.rounded_rectangle(
            [32, 32, 992, 992],
            radius=220,
            fill=(22, 28, 40, 255),
            outline=(55, 75, 105, 255),
            width=12,
        )

        # Inner subtle grid / ring
        d.arc([160, 160, 864, 864], start=0, end=360, fill=(35, 48, 70, 255), width=6)
        d.arc([260, 260, 764, 764], start=0, end=360, fill=(35, 48, 70, 255), width=4)

        # 4-Color Tactical Briefing indicator bands
        # Cyan, Magenta, Amber, Red
        d.line([(160, 820), (320, 820)], fill=(0, 215, 245, 255), width=28)
        d.line([(360, 820), (520, 820)], fill=(240, 50, 215, 255), width=28)
        d.line([(560, 820), (720, 820)], fill=(255, 165, 0, 255), width=28)
        d.line([(760, 820), (864, 820)], fill=(245, 50, 50, 255), width=28)

        # Center stylized airplane glyph
        airplane_pts = [
            (512, 220),   # Nose
            (560, 440),   # Wing root right
            (860, 540),   # Wing tip right
            (860, 580),   # Wing tip trailing
            (560, 540),   # Wing root trailing
            (550, 680),   # Fuselage to tail
            (640, 740),   # Stabilizer right tip
            (640, 770),   # Stabilizer trailing
            (512, 740),   # Tail center
            (384, 770),   # Stabilizer left trailing
            (384, 740),   # Stabilizer left tip
            (474, 680),   # Fuselage to tail left
            (464, 540),   # Wing root trailing left
            (164, 580),   # Wing tip trailing left
            (164, 540),   # Wing tip left
            (464, 440),   # Wing root left
        ]
        d.polygon(airplane_pts, fill=(245, 250, 255, 255))

    with tempfile.TemporaryDirectory() as tmp_dir:
        iconset_dir = Path(tmp_dir) / "AppIcon.iconset"
        iconset_dir.mkdir(parents=True, exist_ok=True)
        sizes = [16, 32, 64, 128, 256, 512]
        for s in sizes:
            resized = base_img.resize((s, s), Image.Resampling.LANCZOS)
            resized.save(iconset_dir / f"icon_{s}x{s}.png")
            if s * 2 <= 1024:
                resized_2x = base_img.resize((s * 2, s * 2), Image.Resampling.LANCZOS)
                resized_2x.save(iconset_dir / f"icon_{s}x{s}@2x.png")

        output_icns_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset_dir), "-o", str(output_icns_path)], check=True)


def build_app_bundle(project_dir: Path) -> Path:
    """Build the macOS .app bundle inside project_dir."""
    app_name = "FAA Plate Enhancer.app"
    app_bundle = project_dir / app_name
    contents_dir = app_bundle / "Contents"
    macos_dir = contents_dir / "MacOS"
    resources_dir = contents_dir / "Resources"

    if app_bundle.exists():
        shutil.rmtree(app_bundle)

    macos_dir.mkdir(parents=True, exist_ok=True)
    resources_dir.mkdir(parents=True, exist_ok=True)

    # 1. Generate icon
    icns_path = resources_dir / "AppIcon.icns"
    generate_app_icon(icns_path)

    # 2. PkgInfo
    (contents_dir / "PkgInfo").write_text("APPL????\n", encoding="utf-8")

    # 3. Info.plist
    plist_content = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleDisplayName</key>
    <string>FAA Plate Enhancer</string>
    <key>CFBundleExecutable</key>
    <string>FAA Plate Enhancer</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>com.antigravity.faaplateenhancer</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>FAA Plate Enhancer</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>CFBundleVersion</key>
    <string>1.0.0</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.15</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
"""
    (contents_dir / "Info.plist").write_text(plist_content, encoding="utf-8")

    # 4. Executable wrapper
    launcher_script = f"""#!/bin/bash
DIR="$( cd "$( dirname "${{BASH_SOURCE[0]}}" )/../../.." && pwd )"
cd "$DIR"

PYTHON_BIN="$HOME/.virtualenvs/plates/bin/python"
if [ ! -f "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(which python3)"
fi

exec "$PYTHON_BIN" "$DIR/desktop_app.py"
"""
    exec_path = macos_dir / "FAA Plate Enhancer"
    exec_path.write_text(launcher_script, encoding="utf-8")
    exec_path.chmod(exec_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    print(f"[+] Successfully built native macOS app bundle: {app_bundle}")
    return app_bundle


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    build_app_bundle(root)
