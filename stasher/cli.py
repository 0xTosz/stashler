"""Command line interface: backfill | live | run | ui."""

from __future__ import annotations

import argparse
import sys
import threading

from . import Stasher, __version__
from .config import Config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stasher", description=__doc__)
    parser.add_argument("--version", action="version", version=f"stasher {__version__}")
    parser.add_argument("--config", help="Path to a TOML config file")
    parser.add_argument("--account", help="Override account name")
    parser.add_argument("--poesessid", help="Override POESESSID")
    parser.add_argument("--league", help="Override league")
    parser.add_argument("--db", dest="db_path", help="Override database path")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backfill", help="Capture currently-listed items, then exit")
    sub.add_parser("live", help="Stream new listings via websocket until interrupted")
    sub.add_parser("run", help="Backfill, then stream live")
    ui_p = sub.add_parser("ui", help="Launch the local web UI")
    ui_p.add_argument("--host", default="127.0.0.1")
    ui_p.add_argument("--port", type=int, default=5000)
    ui_p.add_argument(
        "--reload",
        action="store_true",
        help="Auto-reload on code/template edits (dev only; live capture resets on each change)",
    )

    args = parser.parse_args(argv)

    config = Config.load(
        args.config,
        account_name=args.account,
        poesessid=args.poesessid,
        league=args.league,
        db_path=args.db_path,
    )
    stasher = Stasher(config)
    try:
        return _dispatch(args, stasher)
    finally:
        if args.command != "ui":
            stasher.close()


def _dispatch(args, stasher: Stasher) -> int:
    if args.command == "backfill":
        return _cmd_backfill(stasher)
    if args.command == "live":
        return _cmd_live(stasher)
    if args.command == "run":
        _cmd_backfill(stasher)
        return _cmd_live(stasher)
    if args.command == "ui":
        return _cmd_ui(stasher, args.host, args.port, args.reload)
    return 1


def _cmd_backfill(stasher: Stasher) -> int:
    def progress(label: str, partitions: int, new: int) -> None:
        print(f"  [{partitions:>4} searches | {new:>5} new] {label}", file=sys.stderr)

    print("Backfilling currently-listed items...", file=sys.stderr)
    summary = stasher.backfill(progress=progress)
    print(
        f"Done: {summary['new']} new items, {summary['partitions']} searches across "
        f"{summary['categories']} categories"
        + (f", {summary['incomplete']} partitions hit the cap" if summary["incomplete"] else ""),
        file=sys.stderr,
    )
    return 0


def _cmd_live(stasher: Stasher) -> int:
    stop = threading.Event()
    print("Listening for new listings (Ctrl+C to stop)...", file=sys.stderr)

    def status(**kw) -> None:
        if kw.get("mode") == "live" and kw.get("last_push"):
            print(
                f"  +{kw.get('last_new', 0)} new (pushed {kw['last_push']})",
                file=sys.stderr,
            )
        elif kw.get("mode") == "reconnecting":
            print("  reconnecting...", file=sys.stderr)

    try:
        stasher.run_live(stop, status=status)
    except KeyboardInterrupt:
        stop.set()
        print("\nStopped.", file=sys.stderr)
    return 0


def _cmd_ui(stasher: Stasher, host: str, port: int, reload: bool = False) -> int:
    from .ui.app import create_app

    app = create_app(stasher)
    print(f"stasher UI on http://{host}:{port}", file=sys.stderr)
    # debug=reload enables the werkzeug reloader + Jinja template auto-reload.
    app.run(host=host, port=port, debug=reload, use_reloader=reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
