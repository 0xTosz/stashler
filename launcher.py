"""Entry point for the packaged Stashler desktop app (the PyInstaller target).

Starts the system-tray app using the per-user data directory for storage, so a
double-clicked .exe just works -- the user enters their account/POESESSID in the UI.
On a fatal startup error it shows a Windows message box (the windowed exe has no console).
"""

from __future__ import annotations

import sys
import traceback


def main() -> int:
    try:
        from stasher import Stasher
        from stasher.tray import run_tray

        stasher = Stasher.from_config()  # per-user data dir; credentials set in the UI
        try:
            run_tray(stasher)
        finally:
            stasher.close()
        return 0
    except Exception:  # noqa: BLE001 - surface any startup failure to the user
        _fatal(traceback.format_exc())
        return 1


def _fatal(msg: str) -> None:
    sys.stderr.write(msg)
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None, msg[-1500:], "Stashler — failed to start", 0x10
            )
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
