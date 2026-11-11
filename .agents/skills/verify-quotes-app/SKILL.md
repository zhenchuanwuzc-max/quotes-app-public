---
name: verify-quotes-app
description: Verify the quotes-app browser and data behavior with isolated sample data before handing off changes. Use for changes to this repository.
---

# Verify quotes-app

Read [features/README.md](features/README.md), then the files for the affected user journeys. The main surface is the browser UI served by `server.py`. The macOS wrapper, real synchronization, and actual wallpaper application have separate side effects and are not proved by the isolated checks below.

## Launch and doctor

From the repository root, run the integrated browser proof. Supply a new evidence directory; it refuses to reuse an existing directory.

```bash
/usr/bin/python3 .agents/skills/verify-quotes-app/scripts/prove-browser.py --evidence "$HOME/Library/Caches/quotes-pstack/run-$(date +%Y%m%d-%H%M%S)-$$"
```

The helper creates a temporary HOME, data, backups, and wallpaper directory, copies `quotes.example.json`, selects a free port, and starts `server.py` through `tests/_support.py`. Its doctor confirms that the spawned process is alive, its log names the temporary `quotes.json`, the sample data is readable at `/health`, and the temporary data directory has no `sync.sh`. If any check fails, stop; do not point the browser at the user's running instance. Playwright and its Chromium browser must be installed for `/usr/bin/python3`.

## Drive and evidence

The helper opens the real UI, captures the sample list, searches for `Drucker`, fills and saves a disposable quote, and confirms the text and source in the temporary `quotes.json`. It captures screenshots of the initial state, search, draft, and saved list, plus `result.txt` in the supplied directory. It blocks network requests outside its exact local port and blocks update, sync, and wallpaper routes. Do not treat this as a test of those routes.

For another journey, follow its feature map and extend the disposable browser session or run the referenced existing test. Capture action and outcome, and inspect the isolated data file for mutations. Do not replace the user path with a direct API call and claim the UI was tested. The full existing suite runs with:

```bash
/usr/bin/python3 -m unittest discover -s tests -v
```

`tests/test_data_sync.py` runs release logic in temporary Git repositories with fake service commands. This is not a live release. Never click wallpaper in the isolated app: `/wallpaper` calls macOS to change the actual desktop despite an isolated output directory. Never call `/install-update` or run real `sync.sh` from a verification instance.

For heading and paragraph changes, use [features/headings.md](features/headings.md). The following helper runs the mapped browser journeys and keeps one screenshot per journey plus the test output. Its wallpaper test intercepts the request before it reaches the server, so it verifies image generation without setting the system wallpaper.

```bash
/usr/bin/python3 .agents/skills/verify-quotes-app/scripts/prove-headings.py --evidence "$HOME/Library/Caches/quotes-pstack/headings-$(date +%Y%m%d-%H%M%S)-$$"
```

## Cleanup

The helper stops only its own server and removes its temporary directory in `finally`. It retains the supplied evidence directory. Check the printed cleanup line and confirm the evidence files exist. On a failed run inspect the exception and retained evidence, and use a fresh evidence path for the next attempt. Do not run `scripts/release.sh` without an actual release task.
