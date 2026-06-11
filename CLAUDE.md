# Stashler

Local desktop app that archives your own PoE2 trade listings and scores them against
editable rules. Python package is `stasher`; the product/exe/UI is **Stashler**.
See [README.md](README.md) for the full feature tour and [RELEASING.md](RELEASING.md) for shipping.

## Commands
```bash
pip install -e ".[dev]"        # dev install
python -m pytest               # run tests
python -m stasher.cli --help   # CLI: backfill | live | run | watch | evaluate | ui | tray
pip install -e ".[build]" && python build.py   # -> dist/Stashler.exe
```

## Architecture
- `Stasher` (`__init__.py`) — facade; owns Store, RateLimiter, TradeClient, Evaluator, Pipeline.
- `Worker` (`runtime.py`) — background capture threads + thread-safe status the Flask UI polls.
- `Pipeline` → `Store` (SQLite): two tables, `items` (raw listing+item JSON) and `evaluations`.
- `evaluate/` — `Evaluator` runs each `checks/*` checker; rules are regex / unique_roll / item_filter / archetype_set.
- `ui/` — Flask app + Jinja templates; `tray.py` — pystray desktop wrapper.

## Gotchas
- **Package vs product name:** import/CLI is `stasher` (one s, no l); user-facing is `Stashler`. Don't mix them.
- **Settings live in the SQLite DB**, edited via the UI — not in repo config files. `rules.toml` + the item filter live in the per-user data dir (`%LOCALAPPDATA%\Stashler\` on Windows).
- **The live websocket is intentionally not wired into the UI** — PoE2 encrypts the feed, so capture goes through Auto-refresh (light poll + backfill), never live.
- **Rate limits cause 15–30 min lockouts.** All capture shares one `RateLimiter` + `Pipeline`; don't add a second capture path that spends the budget independently.
- **Bump the version in two places in sync:** `stasher/config.py` (`__version__`) and `pyproject.toml`.
- `evaluate/archetype_model.py` is a **byte-identical vendored copy** of `archetype_miner/model.py` (sync via `python -m tools.vendor`).
