"""Build the standalone Stashler desktop app.

    pip install -e ".[build]"     # pyinstaller + runtime deps
    python build.py               # -> dist/Stashler.exe (Windows)

Generates the .ico from the bundled logo, then runs PyInstaller with stashler.spec.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    png = ROOT / "stasher" / "ui" / "static" / "stashler.png"
    ico = ROOT / "stashler.ico"
    if not ico.exists():
        from PIL import Image

        print("Generating stashler.ico from the logo…")
        Image.open(png).save(
            ico, sizes=[(256, 256), (64, 64), (48, 48), (32, 32), (16, 16)]
        )
    print("Running PyInstaller…")
    subprocess.check_call(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "stashler.spec"], cwd=ROOT
    )
    exe = ROOT / "dist" / ("Stashler.exe" if sys.platform == "win32" else "Stashler")
    print(f"\nDone -> {exe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
