# Instructions for agentic coding tools

This repository is a personal archival tool for Character.AI accounts. If a
user asks you to back up their account, follow this file.

## Status: frozen snapshot (2026-11-14)

The script works the same way the browser does and was verified on
2026-11-14. There is no support, no update, and no maintenance plan. If the
site changes and the tool breaks, it is dead: do not guess new requests or
patch it blindly, and tell the user it is no longer compatible.

## Safety contract

- Only operate on the account of the user who asked. Never use a token that
  was not provided by this user, and never operate on someone else's account.
- Default to liked-only mode (at most 10 characters). Pass `--all` only if the
  user explicitly asks for a complete account backup; never add it unprompted.
- Never print, log, commit, or paste the token. Keep it out of commit
  messages, issues, README files, and chat summaries.
- Never commit `export/`, `test-export/`, `pages/`, `token.txt`, `.env`, or
  any backup output. Check `git status` before committing anything.
- Do not weaken throttling, retries, timeouts, or the character pause. Do not
  add concurrency, parallel requests, or account enumeration.
- Do not modify the tool to access other users' accounts, bypass paywalls or
  age gates, or redistribute character content.
- Do not upload any output to third-party services or turn it into a hosted
  service.

## GUI

`cai_export_gui.py` is a tkinter front-end with the same safety rules. Build it
with:

```bat
pyinstaller --noconfirm --onefile --console --name cai-backup-gui --hidden-import cai_export cai_export_gui.py
```

Do not change it to save the token; `settings.ini` must contain only the output
folder and the full-backup flag.

## Setup

1. `python3 -m pip install -r requirements.txt`
2. Ask the user for their token. Give them the short DevTools steps from
   `README.md` (F12 -> Network -> click any item -> Headers -> `set-cookie`
   or `authorization` -> `Token ...`).
   If they do not want to paste it into chat, ask them to create `token.txt`
   themselves (`chmod 600 token.txt`) and tell you when it is ready.

## Workflow

1. Verify the token without writing output:
   `python3 cai_export.py --token-file token.txt --dry-run`
   Without `--all` this reports liked-only counts; with `--all`, the whole
   account. Report the username and counts. Stop on HTTP 401 and ask for a
   fresh token.
2. Smoke test into a temporary directory:
   `python3 cai_export.py --token-file token.txt --max-chats 2 --out test-export`
3. Backup:
   - default (liked only, at most 10):
     `python3 cai_export.py --token-file token.txt --out export`
   - full account, only if the user explicitly asked: add `--all`
   The full run can take tens of minutes. Run it in the background or with a
   long timeout, and report progress from the log.
4. Verify: read `export/<username>/_manifest.json` and `INCOMPLETE.md`, list
   the produced zip, and report character / chat / message counts.
5. Delete `token.txt` if you created it. Remind the user that logging out
   invalidates the token.

## If something fails

- HTTP 401: token expired or invalid. Ask for a fresh one; do not retry in a
  loop.
- HTTP 429 or repeated 5xx: stop, wait, then rerun with a larger delay
  (e.g. `--rate 1.0`). Never parallelise.
- Missing chats: `STATE_REMOVED` legacy chats are unrecoverable. Mention them
  from `INCOMPLETE.md` instead of retrying.
- Do not "fix" failures by disabling pauses or retries.

## Output

Everything stays local. `user.json` never contains the token. Never publish
the backup and never expose it over a network.
