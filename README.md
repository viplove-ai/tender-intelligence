# Tender Intelligence System

A local-first Streamlit application for importing CPWD tender reports, analysing awards and contractors, extracting text-based NIT PDFs, planning BOQ costs, and producing transparent historical bid estimates.

## Data and privacy model

The durable database is saved in the current browser's IndexedDB. Streamlit sends uploaded files and the browser database to its Python server for processing:

- When you run the application locally with the configuration in this repository, that server is this computer and listens only on `localhost`.
- Each browser session receives an isolated temporary SQLite database. Its temporary directory is removed when the session is released.
- The original uploaded spreadsheet or PDF is not retained. Parsed tender records, saved analyses, and BOQ plans are retained in the browser database.
- Browser storage is not encrypted by this application. Anyone with access to the browser profile may be able to access it.
- If the server binding is changed or the application is deployed remotely, uploaded data is transmitted to and processed by that host. Add authentication and review the host's retention controls before doing this with sensitive data.

Clearing site data for the app's origin removes its browser database. Download a database backup before clearing browser data or permanently deleting tenders.

## Requirements

- Python 3.11 or newer
- A current web browser
- macOS, Windows, or Linux

Production dependencies are deliberately pinned in `requirements.txt`, with the tested transitive graph constrained by `constraints.txt`. Development and security tools are kept separately in `requirements-dev.txt` so they are not installed in production.

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
