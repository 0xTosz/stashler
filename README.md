# Stashler

An append-only archive + local evaluator for **your own** Path of Exile 2 trade listings.

Stashler captures every item you list on the PoE2 trade site exactly once, while it's
listed, stores it in a local SQLite database, and flags the few worth a closer look. It
never re-queries an item it already has, and it is deliberately conservative about the
trade API rate limits (exceeding them earns a 15–30 minute lockout).

> The app is branded **Stashler**; the Python package, CLI module, and database file are
> still named `stasher` (so existing `stasher.db` archives keep working). Run it with
> `python -m stasher.cli …` (or the `stasher` console script).

## How it works

- **Backfill** — enumerates items you currently have listed via the trade search API.
  The search only returns ~100 results per query, so stasher partitions each search by
  item category and (recursively) by item level / rarity to capture everything.
- **Live** — opens the trade live-search websocket, which *pushes* the id of each new
  item as you list it. This is the cheap path that keeps the archive complete over time.
- Both feed a single pipeline that fetches item details (max 10 per request, rate
  limited) and `INSERT OR IGNORE`s them keyed on the item hash, so known items are free.

Because PoE2 is in beta there is no stash API; the trade endpoints are the only option.

## Setup

```bash
pip install -e .            # or: pip install -e ".[dev]" for tests
cp config.example.toml config.local.toml   # then edit it
```

Set your `account_name` (the seller name **including** the `#discriminator`, e.g.
`YourName#1234` — the search needs it), `poesessid` (the POESESSID cookie from
pathofexile.com) and `league`. You can also set these later from the UI.
`config.local.toml` and `*.db` are gitignored — your session id never gets committed.

## Usage

```bash
stasher tray          # run in the system tray (Open UI / Quit) — best for desktop use
stasher ui            # local web UI on http://127.0.0.1:7137
stasher backfill      # one-shot: capture currently-listed items
stasher watch         # near-live: periodic newest-first poll + occasional full backfill
stasher live          # (experimental) connect the live websocket — see note below
stasher run           # backfill, then live
stasher evaluate      # (re)score archived items against rules.toml; --force re-checks all
```

### Standalone desktop app (no Python needed)

Non-technical users can run a single executable — no terminal, just a tray icon:

```bash
pip install -e ".[build]"     # pyinstaller + tray deps
python build.py               # -> dist/Stashler.exe
```

Double-clicking `Stashler.exe` puts a **Stashler** icon in the system tray; its menu has
**Open Stashler** (opens the browser to the UI) and **Quit**. Account / POESESSID / league
are entered in the UI (Settings), so nothing needs editing by hand.

### Where data is stored

By default the database, `rules.toml`, and the filter live in a per-user data directory
(so a packaged app never writes next to its executable):

| OS | Location |
|----|----------|
| Windows | `%LOCALAPPDATA%\Stashler\` |
| macOS | `~/Library/Application Support/Stashler/` |
| Linux | `$XDG_DATA_HOME/Stashler/` (or `~/.local/share/Stashler/`) |

Override with `--db <path>`, `STASHER_DATA=<dir>`, or `data_dir` / `db_path` in config.
The UI prints the active data dir on launch. (A `rules.toml` in your *current directory*
still takes precedence in dev.) The default UI port is **7137** (`--port` to change).

### Keeping the archive current

Use **`stasher watch`** (or the **Auto-refresh** toggle in the UI). It always leads with
a cheap newest-first "light poll" every few minutes — one account search, then fetches
only the listings you don't already have. A full adaptive backfill runs **only when a
poll signals it might have missed something** — its whole newest page was new *and* you
have more active listings than fit on one page. That same signal seeds a cold archive
automatically (an account with ≤ one page of listings is fully captured by the poll
alone), so the heavy scan never runs on a timer or on restart. Repeat runs are nearly
free: already-archived items are skipped, so only genuinely new listings get fetched.

Tuning (`config.local.toml`): `auto_poll_interval` (seconds between light polls, default
180, min 30); `auto_full_interval` (optional timed full-backfill safety net — `0`
disables it, which is the default since overflow is detected automatically).

If the archive ever looks wrong (e.g. you interrupted the very first sync), **Settings →
Danger zone → Force resync** wipes the archived items + evaluations and re-fetches
everything from scratch (your settings and rules are kept).

> **Live websocket (`stasher live`) is experimental on PoE2.** It connects and
> authenticates, but PoE2 delivers new-item notifications as an *encrypted* payload
> (the official web client decrypts it client-side), so the ids can't be read by a
> third-party tool. `stasher watch` is the supported way to stay current.

### As a library

```python
from stasher import Stasher

s = Stasher.from_config("config.local.toml")
s.backfill()                 # synchronous, rate-limited
s.run_live(stop_event)       # blocking; run in a thread if you need to keep going
print(s.db_path)             # query the SQLite file directly from your other tools
```

## Evaluation & review queue

Listing prices are not a reliable signal, so stasher scores each captured item locally
against a chain of editable rules. If **any** rule fires, the item is flagged and shows
up in the **Queue** view of the UI with a plain-English reason. Everything is offline —
it reads only the archived item JSON (affix text, per-affix tier/roll ranges, base, ilvl,
unique name). New items from live + backfill are scored the moment they're stored.

The queue is a manual shortlist: most captures are trash, so the default rules are tight
and aim to surface only the small fraction worth a look. In the UI you can **Mark seen**
(reviewed items sink to the bottom), **Mark all seen**, or **Show all evaluated** to see
below-threshold items too. The nav badge shows the unseen count.

Rules live in an editable `rules.toml` (resolution order: `rules.local.toml` →
`rules.toml` → packaged default). Three checker types:

```toml
[[regex]]                  # match name / base / affix text (in-game wording)
name = "High flat life (T1)"
pattern = "\\+(12\\d|1[3-9]\\d|[2-9]\\d\\d) to maximum Life"
targets = ["affixes"]      # affixes | name | base

[[unique_roll]]            # uniques rolling near the top of their ranges
name = "High-roll unique"
min_percent = 90
aggregate = "avg"          # avg | max | all

[item_filter]              # a single app-managed loot-filter file (edit it in the UI)
enabled = true             # Show/Hide blocks: BaseType, Class, Rarity, ItemLevel, Quality, Sockets
```

Edit the rules, then run `stasher evaluate` to re-score the archive (it only re-checks
items whose stored verdict predates your edit; `--force` re-checks everything). Tune the
thresholds to your league — the shipped patterns are starting points, not gospel.

You can also edit everything from the UI: **Settings → Evaluation rules** has an editor for
the rules and for the single filter file (upload a `.filter` to replace its contents).
**Save** validates, persists, and re-scores the whole archive in one step, so the queue
always matches what you saved. Invalid edits are rejected with an error rather than silently
disabling evaluation.

## Data

Everything lands in the `items` table: item hash (primary key), account, listed/fetched
timestamps, price, name/type/rarity, whisper, and the full raw JSON blob of the
listing+item for anything else you need downstream. Evaluation verdicts (flagged, reasons,
seen) live in a separate `evaluations` table keyed by item hash — derived and safe to
delete/recompute via `stasher evaluate --force`.
