"""System-tray launcher.

Runs the local web UI in a background thread and shows a tray icon whose menu offers
**Open Stashler** (opens the browser) and **Quit**. This is how the packaged desktop
build runs, so non-technical users get a normal tray app instead of a terminal window.

Uses ``werkzeug.serving.make_server`` (not ``app.run``) so the server can be shut down
cleanly when the user picks Quit.
"""

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

from .config import DEFAULT_UI_PORT

# Resolves in both dev and a PyInstaller bundle (datas keep the package layout).
ICON_PATH = Path(__file__).resolve().parent / "ui" / "static" / "stashler.png"


def run_tray(
    stasher,
    host: str = "127.0.0.1",
    port: int = DEFAULT_UI_PORT,
    open_browser: bool = True,
) -> None:
    """Blocking: serve the UI and run the tray icon until the user picks Quit."""
    import pystray
    from PIL import Image
    from werkzeug.serving import make_server

    from .ui.app import create_app

    app = create_app(stasher)
    server = make_server(host, port, app, threaded=True)
    threading.Thread(
        target=server.serve_forever, name="stashler-ui", daemon=True
    ).start()

    shown_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{shown_host}:{port}"
    image = Image.open(ICON_PATH)

    def do_open(icon=None, item=None):
        webbrowser.open(url)

    def do_quit(icon, item):
        server.shutdown()
        icon.visible = False
        icon.stop()

    icon = pystray.Icon(
        "Stashler", image, "Stashler",
        menu=pystray.Menu(
            pystray.MenuItem("Open Stashler", do_open, default=True),
            pystray.MenuItem("Quit", do_quit),
        ),
    )

    if open_browser:
        do_open()
    icon.run()  # blocks until Quit; the caller closes `stasher`
    server.shutdown()
