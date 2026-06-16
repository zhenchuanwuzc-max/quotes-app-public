# quotes-app

A tiny, local-first **quote library** — a single-file web app to collect, browse, and pin short quotes you want to keep. Built to be dead simple: one JSON file is the source of truth, a small Python server serves a single HTML page, and multi-device sync runs over a private Git repo with conflict-free JSON merging.

> This is the open-source version. It ships with example data only — bring your own quotes.

## Why

Most note/quote tools are either too heavy (a database, an account, a cloud service) or too lossy (a plain text file that two devices fight over). This app picks a middle path:

- **JSON-as-truth** — `quotes.json` *is* the database. No SQLite, no binary blobs. Human-readable, diffable, mergeable.
- **Conflict-free sync** — a custom Git merge driver (`json-merge.py`) does a set-union merge on the quote list, so two machines editing in parallel never produce conflict markers.
- **Local-first** — runs entirely on `localhost`. No external service required for the core app.

## Features

- Browse quotes, full-text search across text / source / use-case.
- Pin (favorite) quotes to the top.
- Add quotes from the browser, or via a small HTTP API.
- Multi-device sync over your own **private** Git repo.
- Optional menu-bar packaging on macOS (via `py2app`).

## Architecture

| Piece | What it does |
|---|---|
| `server.py` | Pure-stdlib Python HTTP server. Serves the SPA, exposes `/quotes` read/write API, runs the sync. |
| `index.html` | The entire frontend — one self-contained page, no build step. |
| `quotes.json` | The data. Single source of truth. (This repo ships `quotes.example.json`.) |
| `json-merge.py` | Git merge driver: set-union merge of the quote list so parallel edits never conflict. |
| `sync.sh` | Pull + merge + commit + push against your private data repo. SSH-key first, HTTPS+token fallback. |
| `setup.py` / `*.plist` | Optional macOS `py2app` packaging + `launchd` agents to keep it running. |

## Quick start

```bash
# 1. clone
git clone https://github.com/YOUR_GITHUB_USER/quotes-app.git
cd quotes-app

# 2. seed your data from the example
cp quotes.example.json quotes.json

# 3. run
python3 server.py
# open http://localhost:8767
```

That's the whole app. No dependencies beyond the Python standard library.

## Multi-device sync (optional)

Sync stores your `quotes.json` in a **separate private Git repo** (so your code stays public and your quotes stay private):

1. Create a private repo, e.g. `YOUR_GITHUB_USER/quotes-data`.
2. Point `sync.sh`'s `REPO` at it, and run it from the data directory.
3. `sync.sh` self-heals: it registers the `quotes-union` merge driver, prefers your SSH key, and falls back to an HTTPS personal-access-token if SSH isn't set up.

The union merge driver means you can edit on two machines offline and both edits survive the next sync — no manual conflict resolution.

## Configuration

Replace the placeholders before using:

- `YOUR_GITHUB_USER/quotes-app` — your fork of this code repo (in `server.py`, used for update checks).
- `YOUR_GITHUB_USER/quotes-data` — your private data repo (in `sync.sh`).
- `Your Name` / `you@example.com` — the Git identity used for sync commits.
- `com.example.quotes-*` — launchd labels / bundle ids, if you package it.

## License

MIT — see `LICENSE`.
