# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local-first Streamlit app for importing CPWD tender reports, analyzing awards and
contractors, extracting text-based NIT PDFs, planning BOQ costs, and producing historical
bid estimates. It ships two ways from one codebase (`app.py` + `src/`):

1. **Server mode** — `streamlit run app.py`, a normal Python/pip Streamlit server. This is
   also what Streamlit Community Cloud would run unmodified.
2. **Desktop app** — the same `app.py`/`src/` compiled to run under Pyodide/WebAssembly via
   `@stlite/desktop`, wrapped in Electron, producing an installable `.dmg`/`.exe` with no
   Python install required.

The two runtimes share all business logic and differ only in how the SQLite database
persists — see "Persistence model" below, which is the single most important thing to
understand before touching data-handling code.

## Commands

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt

# Run the app (server mode, the normal dev workflow)
python -m streamlit run app.py                      # http://localhost:8501

# Tests
python -m pytest -q                                  # full suite (~54 pass, 5 skipped)
python -m pytest tests/test_importer.py -q           # single file
python -m pytest tests/test_importer.py::test_name -q  # single test

# Lint / security / dependency checks
ruff check app.py src tests
bandit -q -r app.py src -x tests
pip-audit -r requirements.txt
python -m pip check

# Desktop build (requires Node 20+/22, npm)
npm install
npm run dump && npm run serve   # fastest inner loop: launch via Electron, no installer
npm run dump && npm run app:dir # unpacked app dir, no installer (faster than app:dist)
npm run app:dist                # packaged .dmg (macOS) or .exe (Windows) installer in dist/
```

The 5 skipped tests are PDF integration tests needing real-world fixtures not in the repo —
expected, not a failure.

`npm run dump` is a **snapshot**, not a live mount: rerun it after any change to `app.py`,
`src/*.py`, `master_data/*.xls`, or `stlite.desktop.dependencies`/`stlite.desktop.files` in
`package.json` before testing the desktop build.

**Desktop build gotcha**: if `npm run serve` throws
`Cannot read properties of undefined (reading 'enableSandbox')`, something in the shell has
`ELECTRON_RUN_AS_NODE=1` set, forcing Electron to run as plain Node. Unset it
(`env -u ELECTRON_RUN_AS_NODE npm run serve`) and retry.

## Persistence model — read this before touching data code

Where the durable SQLite database lives depends entirely on the runtime, gated by
`src/persistence.py`'s `IS_WASM` flag (`sys.platform == "emscripten"`), which is the single
call-site the rest of the codebase branches on:

- **Server mode**: each browser session gets an isolated temp SQLite file
  (`app.py`'s `_bootstrap_session_database`). After every mutation, `sync_after_write()` →
  `sync_to_browser()` uses SQLite's **backup API** (not a raw file read — WAL means recent
  commits live in the `-wal` sidecar and a raw read would persist a stale snapshot) to push
  the db into the browser's IndexedDB via `src/browser_sync.py` and `streamlit-js-eval`. On
  load, `load_db_bytes_from_browser()` round-trips it back — this is async, so the first
  script run shows a loader and `st.stop()`s until the component replies and triggers a
  rerun.
- **Desktop app**: no server, no browser storage. `package.json`'s
  `stlite.desktop.nodefsMountpoints` mounts Electron's per-app `userData` directory onto the
  Pyodide virtual filesystem at `/mnt`; `resolve_data_dir()` in `persistence.py` points the
  database straight there. Writes land on disk immediately — `sync_after_write()` is a no-op
  in this mode.
- `src/database.py`'s `_configure_journal_mode` tries WAL and falls back to `DELETE` mode
  under WASM, since WAL needs shared-memory support that WASM filesystem mounts (NODEFS/
  IDBFS) may not provide — SQLite doesn't raise when it can't switch, it silently keeps the
  old mode, so the code checks the reported mode rather than catching an exception.
- Every mutating action in `app.py` funnels through `sync_after_write()` — never call
  `sync_to_browser()` or touch the database directly and skip it, or server-mode changes
  won't survive a refresh.

## Desktop build constraints (`stlite.desktop.dependencies` in `package.json`)

These are resolved by Pyodide's `micropip`, a materially different resolver from
`requirements.txt`'s pip — version pins that work in one often don't work in the other:

- `sqlite3` must be listed explicitly — Pyodide's default runtime omits it even though it's
  stdlib everywhere else.
- `plotly` is pinned to `5.24.1` there vs `6.9.0` in `requirements.txt` — Plotly 6.x's wheel
  fails micropip's resolution step under the currently-bundled Pyodide version. Retest with
  6.x whenever `@stlite/desktop` is upgraded.
- `@stlite/desktop` must be recent enough to bundle a Streamlit that understands `width=`
  (this codebase uses it instead of `use_container_width=`); this repo pins `^0.101.1`
  (bundles Streamlit 1.57).
- `streamlit-js-eval` is deliberately excluded from the desktop dependency list — it isn't
  installable under Pyodide, which is exactly why the desktop build doesn't need it (see
  Persistence model above).
- No other desktop dependency versions are pinned — Pyodide only ships wheels for specific
  package/version combinations, so tight pinning tends to fail rather than protect. Verify
  with `npm run dump` after any dependency or `@stlite/desktop` upgrade.
- `electron-builder`'s `artifactName` (in `package.json`'s `build.mac`/`build.win`) is
  pinned to fixed, version-free filenames (`Tender-Intelligence.dmg`,
  `Tender-Intelligence-Setup.exe`) so `.../releases/latest/download/<name>` is a permalink
  that always resolves to the newest release without any code change.

## The desktop app's own update mechanism

There's no silent auto-update (that needs paid code-signing certs — Apple Developer ID +
notarization, a Windows EV cert — which this project doesn't have; see
`DESKTOP_RELEASE_GUIDE.md` for what that would take). Instead, `app.py`'s masthead has a
"Check for updates" button (desktop build only) that:

1. Compares its own version (read from the bundled `package.json` — it's listed in
   `stlite.desktop.files` specifically so it ships next to `app.py`) against GitHub's
   latest release tag, fetched via `pyodide.http.pyfetch` (Pyodide's built-in browser-fetch
   bridge — chosen over adding a `requests`-via-`pyodide-http` dependency).
2. If newer, offers to download and SHA256-verify the installer in-app
   (`download_and_verify_installer`), then hand it to the user via `st.download_button`.
   The checksum is published by `scripts/checksum-installer.js` in CI, right after
   `electron-builder` produces the installer.
3. `_current_desktop_platform()` reads a `desktop_platform.txt` marker baked in at build
   time by `scripts/write-platform-marker.js` (an npm `predump` hook) — **not** detected at
   runtime, because `st.context.headers` inside the desktop shell exposes only a synthetic
   `{'Host': 'stlite.local'}`, no real browser User-Agent. (The web-mode equivalent,
   `_detect_os_download()`, *does* use the real User-Agent — that only works in a real
   browser tab, not inside stlite's internal websocket bridge.)

Every network call in this path is wrapped so failure degrades to a manual link rather than
crashing the app — offline, rate-limited, or no-release-yet are all expected states.

## Data safety limits

Enforced deliberately, not incidental — extend rather than remove when adding features:
uploaded spreadsheets/PDFs capped at 5MB each, PDFs at 500 pages, workbook decompressed-size
and row/column limits, and a 128MB cap on database restore. Imports are transactional and
reconciled by `external_key` (see `src/importer.py`) — reimporting a compatible Tender ID
updates changed fields without duplicating rows, and blank incoming values never overwrite
existing non-blank ones.

## Module map (`src/`)

- `database.py` — connection/transaction management, session-scoped db path via
  `contextvars` (`set_session_db_path`/`get_default_db_path`), contractor
  resolution/aliasing/merging, change history (`record_change`).
- `persistence.py` — the `IS_WASM` runtime flag and `resolve_data_dir()`/
  `sync_after_write()` shim described above; the one file both runtimes' persistence code
  routes through.
- `browser_sync.py` — IndexedDB round-trip encode/decode/validation for server mode.
- `importer.py` — spreadsheet parsing, column matching, dedup/merge logic, transactional
  commit.
- `analysis_store.py` — save/load/list persisted NIT-extraction + BOQ + estimate bundles.
- `nit_parser.py` — text-based NIT PDF field extraction.
- `boq_costing.py` — BOQ category percentages, overhead/logistics/contingency/margin math.
- `prediction.py` — historical-award-based bid estimation (weighted medians/quartiles,
  similarity ranking by description/contractor/work type/office/size/location/recency).
- `guarantee.py` — performance guarantee calculation.
- `analytics.py` — dashboard/contractor metrics and filtering.
- `classifier.py` / `cleaning.py` — work-type classification and text/number/date cleaning
  shared across import and analysis.
- `corrections.py` — bulk delete-by-filter operations.
- `exporter.py` — Excel export and consistent SQLite backup snapshots (also backup-API
  based, for the same WAL reason as `sync_to_browser`).
- `utils.py` — logging setup (stdout, since Community Cloud's disk is ephemeral/shared).

`app.py` is the single Streamlit entrypoint for both runtimes — page routing (`PAGES`/
`ONBOARDING_PAGES`), the masthead/update-check UI, master reset, and the three pages
(Dashboard, Contractors, Tender Analysis & Bid Estimator) all live there and call into
`src/`.
