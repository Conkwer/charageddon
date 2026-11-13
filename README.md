# CharAgeddon

Local backup of **your own** Character.AI account: by default only your liked
characters (Character.AI exposes at most 10), or the whole account with
`--all`. Saves Tavern V2 cards, avatars, lorebooks, and chats (including
archived chats) as SillyTavern-friendly `.jsonl` / `.html` files.

## Status: frozen snapshot (2026-11-14)

As of **2026-11-14** the script works the same way your browser does: it reads
your own account through the same requests the site itself makes. It is
published as-is: **no support, no updates, and no maintenance are planned.**
If Character.AI changes how the site works and the script stops working, the
tool is dead — do not expect a fix. Fork it if you want to keep it alive.

## Read this first

- You authenticate as yourself and read your own account's data. The tool
  makes the same requests to Character.AI that your browser makes while you
  use the site; you are not accessing anybody else's data.
- **No guarantee. Your account can be banned or limited for automated access.
  Use at your own risk.**
- Personal archival of your own data only. Do not redistribute character
  cards, chats, or other creators' content.
- Default scope is the liked list only (at most 10); nothing else is
  enumerated. Pass `--all` for a full account backup.
- Not affiliated with Character.AI. The API is undocumented and may change or
  disappear at any time.
- The repository contains **no tokens, no saved pages, and no exported data**.
  Your token and your backup stay on your machine.
- MIT license.

## Requirements

- Python 3.9+
- `pip install -r requirements.txt` (only `requests` and `Pillow`)

## System requirements

- **Compiled GUI, tested:** Windows 11 x64. The released exe was built there
  with a Python 3.12 x86 (32-bit) fork, so it should be compatible with
  legacy Windows too.
- **From source:** any OS with Python 3.9+ and `Pillow`/`requests` installed.
  The GUI also needs `tkinter`, which ships with the official Windows and
  macOS Python installers; on Linux install `python3-tk` if it is missing.
- **CLI only (`cai_export.py`):** same as source, but without `tkinter`.

## Get your token

1. Log in at <https://character.ai> in Chrome.
2. `F12` → **Network** tab → reload the page (`F5`).
3. Click any item in the list (not a folder), e.g. `ping`.
4. Open the **Headers** tab and find `set-cookie` (Response Headers) or
   `authorization` (Request Headers). The token is the long value after
   `Token `.
5. Copy only that value — stop at `;` if there is trailing cookie text.

The token usually expires after 1 day. If the script reports HTTP 401, repeat
these steps. Treat it like a password: do not paste it into screenshots,
issues, or chat logs.

## Run

```bash
# liked characters only (at most 10), output to ./export
python cai_export.py --token YOUR_TOKEN --out export

# whole account: liked + created + every character you chatted with
python cai_export.py --token YOUR_TOKEN --all --out export
```

Safer token handling (keeps it out of shell history):

```bash
printf '%s\n' YOUR_TOKEN > token.txt
chmod 600 token.txt
python cai_export.py --token-file token.txt --out export
rm token.txt
```

Or set the environment variable `CAI_TOKEN` (comma-separate several tokens).
`--token` is repeatable for multiple accounts.

Start with a cheap check:

```bash
python cai_export.py --token-file token.txt --dry-run
python cai_export.py --token-file token.txt --max-chats 2 --out test-export
```

Then run the full backup. It is resumable: existing raw chat files are reused
unless you pass `--force`.

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--token` | – | API token, repeatable |
| `--token-file` | – | file with one token per line |
| `--account` | all | only these usernames, repeatable |
| `--all` | off | whole account, not just the liked characters |
| `--out` | `export` | output directory |
| `--rate` | `0.4` | base delay between requests, ±30% jitter |
| `--pause-every` | `10` | pause after N characters; `0` disables |
| `--pause-seconds` | `15` | length of that pause |
| `--timeout` | `90` | read timeout, seconds (connect is 10) |
| `--cards-only` | off | skip chats |
| `--chats-only` | off | skip cards/lorebooks |
| `--max-chats` | unlimited | stop after N chats (testing) |
| `--force` | off | re-fetch chats that already exist |
| `--dry-run` | off | inventory only, write nothing |
| `-v` | off | verbose retries |

## GUI version (Windows, PyInstaller)

`cai_export_gui.py` is a tkinter front-end: token field, output folder, and a
"Full backup" checkbox. It shows the same log in the window and in the
console, and every 30 characters it asks **"N characters are backupped, do
you want to continue?"**. The token is kept in memory only; only the output
folder and the checkbox are remembered in `settings.ini`.

Run from source:

```bash
python cai_export_gui.py
```

Build a single executable on Windows (the console stays visible so progress
can be watched):

```bat
py -m pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --console --name cai-backup-gui ^
    --hidden-import cai_export cai_export_gui.py
```

Run this from the folder containing **both** `cai_export_gui.py` and
`cai_export.py`; `--hidden-import cai_export` makes sure the exporter is
bundled. On Windows you can just run `build_gui.bat`, which checks both files
and uses this command.

The exe appears in `dist\`. Swap `--console` for `--windowed` if you prefer no
console window; the in-window log still works. Windows SmartScreen may warn
about an unsigned executable.

If you see `ModuleNotFoundError: No module named 'cai_export'`, the exe was
built without the exporter. Rebuild with the command above, or put
`cai_export.py` next to the exe.

## What you get

```
export/<username>/
  cards/<Name>.card.json        Tavern Character Card V2
  cards/<Name>.card.png         V2 card embedded in PNG (chara chunk)
  cards/<Name>.avatar.<ext>     original avatar (animation preserved)
  chats/<Name>/YYYYMMDD-HHMM.jsonl   SillyTavern chat
  chats/<Name>/YYYYMMDD-HHMM.html    readable transcript
  chats/<Name>/YYYYMMDD-HHMM.raw.json
  chats/<Name>/_index.json
  lorebooks/<Name>.json
  user.json, personas.json, _manifest.json, INCOMPLETE.md
  README.txt                    what to import, and what not to
  <username>-cai-backup-<timestamp>.zip
```

By default only your liked characters are exported (at most 10). With
`--all`, characters found = liked + created + everyone you have a chat with
(including characters you never liked or created). Archived chats are
temporarily unarchived, fetched, and re-archived.

## Throttling and server load

The defaults are deliberately conservative:

- 0.4 s between requests, with ±30% random jitter.
- Exponential backoff on `429`, `500`, `502`, `503`, `504`, honouring
  `Retry-After`.
- 15 s pause after every 10 characters.
- 10 s connect / 90 s read timeout.

A full account is hundreds of requests; expect tens of minutes. Do not lower
the delays to "finish faster". If you see repeated `429` responses, stop and
raise `--rate` (e.g. `--rate 1.0`).

## Limitations

- `STATE_REMOVED` chats (legacy 2023 conversations never migrated) cannot be
  fetched by any client; they are listed in `INCOMPLETE.md`.
- Deleted/private characters are saved as chats only; the name is recovered
  from message authors and the card is minimal.
- Images are downloaded at the CDN's largest non-AVIF render (≤400 px), which
  is what the CDN serves.
- Default mode exports only the liked list; created or chatted-with
  characters require `--all`.
- Desktop only. Android/iOS cannot run this or a desktop extension.
- Chat search, liked-list expansion beyond 10, and account enumeration for
  other users are out of scope by design.

## License

MIT — see `LICENSE`.
