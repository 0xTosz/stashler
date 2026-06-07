# Stashler

**Find the good items hiding in your own Path of Exile 2 trade listings.**

Stashler quietly archives every item you have listed on the PoE2 trade site, then scores
each one against editable rules and surfaces the small fraction worth a closer look — so
you don't have to eyeball hundreds of listings by hand. It runs locally as a small desktop
app with a system-tray icon and a browser UI; nothing leaves your machine.

![Stashler overview](docs/screenshots/hero.png)

> Branded **Stashler**; the Python package / CLI / database are still named `stasher`
> internally, so existing `stasher.db` archives keep working.

---

## Get started

### Option A — standalone app (no Python)

1. Grab / build `Stashler.exe` (see [Building the .exe](#building-the-exe)) and double-click it.
2. A **Stashler** icon appears in the system tray → **Open Stashler** opens the UI in your browser.
3. In **Settings**, enter your **account name**, **POESESSID**, and pick your **League**
   (there's an in-app guide for finding the POESESSID), then click **Test connection**.
4. Hit **Auto-refresh** in the top bar. That's it — items start flowing into Records, and
   anything noteworthy appears in the Queue.

### Option B — from source

```bash
pip install -e .
python -m stasher.cli tray      # tray app   (or: ... ui  for just the browser UI)
```

The UI runs at `http://127.0.0.1:7137`. Everything (credentials, rules, league) is
configured in the UI — no config files to edit.

---

## The UI

### Records — browse the whole archive

A fast, in-memory table of every captured item. Search by name/type, filter by rarity or
"flagged only", and sort any column (including **Matches** — how many rules an item hit).
Click a row to expand a trade-style tooltip card: properties, item level, implicit/explicit
mods with their **P#/S# tier tags**, DPS, and exactly why it was flagged.

![Records](docs/screenshots/records.png)

### Queue — your review shortlist

The items worth a look. Each flagged item is a card showing the item and a **tile per rule
match** ("Flagged · 3 matches"). Sort by **newest** or **most matches**, **Mark seen** to
sink reviewed items (they fade in place), or **Mark all seen**. The nav badge — and your
browser tab title — show the unseen count, so a backgrounded tab still nudges you.

![Review queue](docs/screenshots/queue.png)

### Settings — credentials, league, and rules

Enter your account name + POESESSID (with a built-in "how to get your POESESSID" guide),
choose your league from a **live dropdown** fetched from the trade site, and **Test
connection** to confirm it all works. Below that, an editor for your **evaluation rules**
and the **item filter** — **Save** validates and re-scores the whole archive in one click.
A **Danger zone → Force resync** wipes and re-fetches the archive if it ever looks wrong.

![Settings](docs/screenshots/settings.png)

The top bar also has a **Log** view (the raw query feed, handy for diagnostics) and a
live rate-limit display — Stashler is deliberately conservative with the trade API
(exceeding the limits earns a 15–30 minute lockout).

---

## How evaluation works

Listing prices aren't reliable, so Stashler ignores them and scores each item **locally**
against a chain of rules — reading only the archived item data (affix text, per-affix
tier/roll ranges, base, item level, unique name). If **any** rule matches, the item is
flagged with a plain-English reason. Most captures are trash, so the shipped rules are
tight and aim to surface only the small fraction worth checking.

![Evaluation rules](docs/screenshots/evaluation.png)

Three checker types, all edited from **Settings** (or in `rules.toml`):

```toml
[[regex]]                  # match name / base / affix text (in-game wording)
name = "High flat life (T1)"
pattern = "\\+(12\\d|1[3-9]\\d|[2-9]\\d\\d) to maximum Life"
targets = ["affixes"]      # affixes | name | base

[[unique_roll]]            # uniques rolling near the top of their ranges
name = "High-roll unique"
min_percent = 90
aggregate = "avg"          # avg | max | all

[item_filter]              # a single loot-filter file, edited/uploaded in the UI
enabled = true             # Show/Hide blocks: BaseType, Class, Rarity, ItemLevel, Quality, Sockets
```

Tune the thresholds to your league — the defaults are starting points, not gospel. Saving
in the UI re-scores the whole archive, so the Queue always matches your current rules.

---

## Keeping the archive current

Click **Auto-refresh** (or run `stasher watch`). It leads with a cheap newest-first "light
poll" every few minutes — one account search, then it fetches only listings you don't
already have. A full adaptive backfill runs **only when a poll detects it might have missed
something**, never on a timer; that same signal also seeds a fresh archive automatically.
Repeat runs are nearly free.

> The trade **live websocket** is *not* used: on PoE2 the new-item feed is encrypted (only
> the official web client can read it), so Auto-refresh is the supported way to stay current.

---

## Where your data lives

The database, rules, and filter live in a per-user data directory, so the packaged app
never writes next to its executable:

| OS | Location |
|----|----------|
| Windows | `%LOCALAPPDATA%\Stashler\` |
| macOS | `~/Library/Application Support/Stashler/` |
| Linux | `$XDG_DATA_HOME/Stashler/` (or `~/.local/share/Stashler/`) |

Your POESESSID never leaves this machine. Override storage with `--db`, `STASHER_DATA`, or
`data_dir`/`db_path` in config; the UI prints the active data dir on launch.

---

## For developers

```bash
pip install -e ".[dev]"     # dev deps (pytest)
python -m pytest            # run the tests
python -m stasher.cli --help
```

CLI commands (the UI/tray cover everyday use; these are for scripting/headless):

| command | what it does |
|---------|--------------|
| `tray` | system-tray app (Open UI / Quit) |
| `ui` | local web UI on `:7137` (`--port` to change) |
| `watch` | auto-refresh loop (light poll + occasional full backfill) |
| `backfill` | one-shot capture of currently-listed items |
| `evaluate` | re-score the archive against the rules (`--force` re-checks all) |

As a library:

```python
from stasher import Stasher
s = Stasher.from_config()    # uses the per-user data dir
s.backfill()                 # synchronous, rate-limited
print(s.db_path)             # query the SQLite file directly
```

### Building the .exe

```bash
pip install -e ".[build]"   # pyinstaller + tray deps
python build.py             # -> dist/Stashler.exe (windowed tray app)
```

### Data model

Captured items land in the `items` table (hash PK, account, timestamps, price,
name/type/rarity, whisper, and the full listing+item JSON). Evaluation verdicts live in a
separate `evaluations` table (flagged, reasons, seen) — derived and safe to recompute with
`stasher evaluate --force`.
