# stasher

An append-only archive of **your own** Path of Exile 2 trade listings.

Stasher captures every item you list on the PoE2 trade site exactly once, while it's
listed, and stores it in a local SQLite database for downstream tooling. It never
re-queries an item it already has, and it is deliberately conservative about the
trade API rate limits (exceeding them earns a 15–30 minute lockout).

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
stasher ui          # local web UI: browse records, settings, status bar, manual backfill
stasher backfill    # one-shot: capture currently-listed items
stasher live        # connect the websocket and archive new listings until stopped
stasher run         # backfill, then live
```

### As a library

```python
from stasher import Stasher

s = Stasher.from_config("config.local.toml")
s.backfill()                 # synchronous, rate-limited
s.run_live(stop_event)       # blocking; run in a thread if you need to keep going
print(s.db_path)             # query the SQLite file directly from your other tools
```

## Data

Everything lands in the `items` table: item hash (primary key), account, listed/fetched
timestamps, price, name/type/rarity, whisper, and the full raw JSON blob of the
listing+item for anything else you need downstream.
