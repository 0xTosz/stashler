"""Build the standalone Stashler desktop app.

    pip install -e ".[build]"     # pyinstaller + runtime deps
    python build.py               # -> dist/Stashler.exe (Windows)

Generates the .ico from the bundled logo, then runs PyInstaller with stashler.spec.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Modules that must import for PyInstaller to bundle a *working* exe. A thin env (e.g. only
# pyinstaller+pillow) silently produces an exe that crashes on a missing module at launch — the
# pystray gap that shipped in 0.2.0b2. Fail loudly here instead. {import name: pip dist}.
_REQUIRED = {
    "PyInstaller": "pyinstaller", "PIL": "pillow", "pystray": "pystray", "flask": "flask",
    "httpx": "httpx", "websockets": "websockets", "yaml": "pyyaml",
}


def _check_deps() -> None:
    missing = [dist for mod, dist in _REQUIRED.items()
               if importlib.util.find_spec(mod) is None]
    if missing:
        print(f"! Build aborted — missing deps: {', '.join(sorted(missing))}", file=sys.stderr)
        print('  Install the full build environment:  pip install -e ".[build]"', file=sys.stderr)
        raise SystemExit(1)


def main() -> int:
    _check_deps()
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
