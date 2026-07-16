# Tender Intelligence System

A local-first Streamlit application for importing CPWD tender reports, analysing awards and contractors, extracting text-based NIT PDFs, planning BOQ costs, and producing transparent historical bid estimates.

It runs two ways: as a normal Streamlit server (the dev workflow — see **First setup** below), or packaged as an installable Windows/macOS desktop app with no Python install required (see **Desktop app**).

## Data and privacy model

Where the durable database lives depends on how you're running the app:

**Server mode** (`python -m streamlit run app.py`): the database is saved in the current browser's IndexedDB. Streamlit sends uploaded files and the browser database to its Python server for processing:

- When you run the application locally with the configuration in this repository, that server is this computer and listens only on `localhost`.
- Each browser session receives an isolated temporary SQLite database. Its temporary directory is removed when the session is released.
- The original uploaded spreadsheet or PDF is not retained. Parsed tender records, saved analyses, and BOQ plans are retained in the browser database.
- Browser storage is not encrypted by this application. Anyone with access to the browser profile may be able to access it.
- If the server binding is changed or the application is deployed remotely, uploaded data is transmitted to and processed by that host. Add authentication and review the host's retention controls before doing this with sensitive data.

Clearing site data for the app's origin removes its browser database. Download a database backup before clearing browser data or permanently deleting tenders.

**Desktop app**: there is no server and no browser storage. Python runs locally inside the app (via Pyodide/WebAssembly), and the SQLite database is a real file on your disk, in the app's data folder (Electron's per-app `userData` directory — e.g. `~/Library/Application Support/tender-intelligence-desktop` on macOS, `%APPDATA%\tender-intelligence-desktop` on Windows). Nothing is sent to any server. Data persists across restarts because it's an ordinary file, not because anything is synced anywhere.

## Requirements

- Python 3.11 or newer
- A current web browser
- macOS, Windows, or Linux

Production dependencies are deliberately pinned in `requirements.txt`, with the tested transitive graph constrained by `constraints.txt`. Development and security tools are kept separately in `requirements-dev.txt` so they are not installed in production.

## Testing it locally

There are three independent things you can run on your machine. Pick based on what you're checking.

### 1. Automated test suite (fastest — run this first)

```bash
python3 -m venv .venv                       # first time only
source .venv/bin/activate                   # Windows: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q                         # should print "N passed, 5 skipped"
ruff check app.py src tests
```

The 5 skips are PDF integration tests that need real-world fixture files not included in
the repo — that's expected, not a failure. See **Development verification** below for the
full check list (`bandit`, `pip-audit`, etc.).

### 2. Server mode (the normal dev workflow)

```bash
source .venv/bin/activate                   # if not already active
python -m streamlit run app.py
```

Opens at <http://localhost:8501>. Walk through **Importing tender reports** and **NIT
extraction and bid estimation** below to exercise the real workflows — upload a bundled
region's data from the Dashboard, refresh the browser, and confirm it's still there
(this is the IndexedDB round trip described in **Data and privacy model**).

### 3. Desktop app (the packaged Electron/Pyodide build)

```bash
npm install                                 # first time, or after package.json changes
npm run dump && npm run serve               # fastest inner loop: launches the app directly,
                                             #   no installer built
```

This is slow the first time (Pyodide + package wheels download), fast after. To test the
actual installer instead of just the dev launch, run `npm run app:dist` and install the
`.dmg`/`.exe` it produces from `dist/`. Either way, the persistence check that matters:
import the bundled sample data, **fully quit the app** (not just close the window — quit
it), relaunch, and confirm the data is still there with no re-import. That's the desktop
build's actual persistence guarantee (a real file on disk, not browser storage) — see
`DESKTOP_RELEASE_GUIDE.md` for how it works and the packaging constraints that make this
build different from server mode's dependency set.

## First setup

macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

The application opens at <http://localhost:8501>. Keep the terminal open while using it and press `Ctrl+C` to stop it.

## Desktop app

The app is also packaged as an installable Windows `.exe` and macOS `.dmg` via [`@stlite/desktop`](https://github.com/whitphx/stlite/tree/main/packages/desktop) (Streamlit compiled to run under Pyodide/WebAssembly, wrapped in Electron). No Python install is required to run the built app — Python itself ships inside it. This is a separate distribution path from the server mode above; both read the same `app.py` and `src/` code, and server mode is unaffected.

See **[DESKTOP_RELEASE_GUIDE.md](DESKTOP_RELEASE_GUIDE.md)** for the full build/release/update walkthrough, including CI and how new versions reach users. The summary below is enough to build one locally.

### Building it

Requires Node.js 20+ (tested with 22) and npm.

```bash
npm install
npm run dump      # bundles app.py, src/, master_data/*.xls and Python deps into build/
npm run app:dist  # packages build/ into an installer for the current OS (dist/)
```

`npm run app:dist` only builds for the OS it runs on — electron-builder cannot reliably cross-build a Windows installer from macOS or vice versa. `.github/workflows/desktop-build.yml` builds both installers on `macos-latest` and `windows-latest` and uploads them as artifacts; treat CI as the release path for distributing both platforms. To preview the app locally without packaging an installer, run `npm run dump && npm run serve`.

### Installing

Run the `.dmg` (macOS) or the NSIS `.exe` installer (Windows) produced above, then launch **Tender Intelligence** like any other installed app.

### Known packaging constraints

`stlite.desktop.dependencies` in `package.json` lists the Python packages the desktop build needs, resolved by Pyodide's package manager (`micropip`) rather than pip — this is a different resolver from `requirements.txt`'s, with its own compatibility quirks:

- **`sqlite3` must be listed explicitly.** Pyodide's default runtime doesn't include it, even though it's part of the Python standard library everywhere else — every part of this app that touches the database needs it.
- **`plotly` is pinned to `5.24.1`, not the `6.9.0` used in server mode.** Plotly 6.x's published wheel fails micropip's wheel-resolution step under the Pyodide version this app currently bundles (`Can't find a pure Python 3 wheel for: 'plotly'`), even though the wheel itself is a normal universal `py3-none-any` build. Retest with plotly 6.x whenever `@stlite/desktop` (and the Pyodide/micropip version it bundles) is upgraded — this may already be fixed upstream.
- **`@stlite/desktop` must be recent enough to bundle a Streamlit release that understands `width=`** (the parameter this codebase's widgets use instead of the older `use_container_width=`). Streamlit versions bundled by very old `@stlite/desktop` releases (pre-1.44-ish) raise `TypeError: ... got an unexpected keyword argument 'width'` at runtime. This repo pins `@stlite/desktop` to `^0.101.1`, which currently bundles Streamlit 1.57.
- No production dependency versions are otherwise pinned for the desktop build (unlike `requirements.txt`) — Pyodide only ships wheels for specific package/version combinations, so pinning tightly tends to fail instead of protect. Verify with `npm run dump` after any dependency or `@stlite/desktop` upgrade.

### Persistence model

Server mode shuttles the SQLite file to the browser's IndexedDB after every write (`src/browser_sync.py`). The desktop build doesn't need this: `package.json`'s `nodefsMountpoints` mounts Electron's `userData` directory onto the Pyodide virtual filesystem at `/mnt`, so `src/persistence.py` points the database straight at a real file under `/mnt` and every write already lands on disk — no sync step, no IndexedDB, no `streamlit-js-eval` (which isn't installable under Pyodide and is deliberately excluded from `stlite.desktop.dependencies`). See `src/persistence.py` for the runtime-detection shim (`IS_WASM`) that the rest of the codebase uses as its single call-site for this.

## Importing tender reports

1. Open **Dashboard**.
2. Upload one or more CPWD `.xls` or `.xlsx` reports, each no larger than 5 MB. Ready-made public CPWD data is also available by region.
3. Optionally enter a Division override.
4. Review the detected fields, new/updated/unchanged counts, warnings, rejected rows, and preview.
5. Press **Confirm Import**.

Imports are transactional. Reimporting a compatible Tender ID updates useful changed values without duplicating it, and blank later values do not erase useful existing values. Workbooks are also subject to decompressed-size, row, and column safety limits.

## Dashboard and contractor analysis

Dashboard filters apply to its metrics, charts, and tender table. The Contractors page provides a searchable profile and comparison tabs: award histories, comparisons for up to five contractors, weighted results, work patterns, and annual trends.

To remove an incorrect office slice, open **Delete tenders by region / office** on the Dashboard:

1. Download the current SQLite backup from the delete panel.
2. Select at least one Region, Zone/Circle, Division, or Sub-division.
3. Verify the displayed deletion count.
4. Tick the permanent-deletion confirmation and delete.
5. Reimport corrected reports if needed.

Selections across multiple hierarchy fields are combined with `AND` logic.

## NIT extraction and bid estimation

1. Open **Tender Analysis & Bid Estimator**.
2. Optionally upload a text-based NIT PDF of up to 5 MB and 500 pages.
3. Review every extracted field and warning. Image-only PDFs require OCR before upload.
4. Correct the tender details, select any historical contractor, and review the planned bid position.
5. For a detected BOQ, review category percentages, high-value overrides, all-item overrides, overhead, logistics, contingency, and target margin.
6. Press **Analyze & Save Tender**.

The estimator ranks historical awards using description similarity, contractor, work type, office, size, location, and recency. It uses weighted medians and weighted quartiles while reducing extreme-tail influence. Results are historical estimates, not guarantees of a future bid.

Saved analyses retain the extracted structured fields, reviewed inputs, BOQ plan, profitability summary, and latest estimate. They do not retain the original PDF or full extracted page text.

## Backups

The delete panel provides **Download database backup**, which creates a consistent SQLite snapshot including committed WAL changes. Its restore expander can integrity-check and restore a downloaded backup up to the 128 MB database safety limit. Spreadsheet and PDF widgets retain their separate 5 MB limits. Keep backups outside browser storage.

The database is browser-backed, so the old project-level `data/tender_intelligence.db` file is not the active database for normal app sessions. Files already present under `data/` are ignored by Git and may be retained only for legacy/manual recovery.

## Development verification

Install development tools separately:

```bash
python -m pip install -r requirements-dev.txt
```

Run the full local verification:

```bash
python -m pip check
python -m pytest -q
ruff check app.py src tests
bandit -q -r app.py src -x tests
pip-audit -r requirements.txt
```

The PDF integration tests use optional real-world fixtures and skip when those files are not present. All other tests use generated data and temporary databases.

## Dependency upgrades

Do not loosen production version ranges for automatic upgrades. To upgrade deliberately:

1. Check the latest stable releases and security advisories.
2. Update the exact pins in `requirements.txt` and `requirements-dev.txt`.
3. Recreate or upgrade the virtual environment.
4. Run every verification command above and the Streamlit app smoke test.
5. Confirm import, deletion/backup, browser persistence, NIT extraction, and saved-analysis workflows manually.

## Troubleshooting

- **`streamlit` is not recognized:** activate `.venv` and use `python -m streamlit run app.py`.
- **The browser does not open:** visit <http://localhost:8501>.
- **Port 8501 is in use:** stop the other process or pass `--server.port 8502`.
- **An old `.xls` file fails:** open it in Excel or LibreOffice and save a clean `.xlsx` copy.
- **A scanned PDF has no text:** run OCR and upload the searchable copy.
- **A page reports an error:** inspect the terminal logs; raw tracebacks are not shown to visitors.
- **Browser data was rejected:** restore a known-good downloaded SQLite backup from the Dashboard or reimport the source reports.
- **`npm install` succeeds but `node_modules/electron/dist` has only a license file, no `Electron.app`:** a very new/nightly Node.js version can silently truncate Electron's own postinstall zip extraction. Use a current LTS Node release (20 or 22) instead.
- **`npm run serve` throws `Cannot read properties of undefined (reading 'enableSandbox')`:** something in your shell has `ELECTRON_RUN_AS_NODE=1` set, which forces Electron to run as plain Node instead of launching its GUI runtime. Unset it (`env -u ELECTRON_RUN_AS_NODE npm run serve`) and retry.
- **Desktop app shows `ModuleNotFoundError` or a Python `TypeError` at startup:** almost always a Pyodide packaging mismatch, not an app bug — see "Known packaging constraints" above.
