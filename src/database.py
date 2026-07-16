from __future__ import annotations

import contextvars
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .cleaning import normalize_contractor_name, parse_publishing_office, preferred_display_name
from .persistence import IS_WASM


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = Path(os.environ.get("TENDER_DB_PATH", BASE_DIR / "data" / "tender_intelligence.db"))

# Each Streamlit session gets its own SQLite file (hydrated from/synced back to the
# browser's IndexedDB — see src/browser_sync.py). Streamlit runs every script rerun in
# its own copied context, so a contextvar set at the top of app.py naturally scopes the
# override to that session's rerun without leaking across concurrently-connected users.
_SESSION_DB_PATH: contextvars.ContextVar[Path | None] = contextvars.ContextVar("session_db_path", default=None)


def set_session_db_path(path: str | Path | None) -> None:
    _SESSION_DB_PATH.set(Path(path) if path is not None else None)


def get_default_db_path() -> Path:
    return _SESSION_DB_PATH.get() or DEFAULT_DB_PATH


def utcnow() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else get_default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    _configure_journal_mode(conn)
    return conn


def _configure_journal_mode(conn: sqlite3.Connection) -> None:
    """WAL needs shared-memory (-shm) support that WASM filesystem mounts (NODEFS/IDBFS)
    may not provide. SQLite doesn't raise when it can't switch modes — it silently keeps
    the previous mode and reports that back — so check the reported mode rather than
    catching an exception; also guard the call itself in case a mount rejects it outright.
    """
    if not IS_WASM:
        conn.execute("PRAGMA journal_mode=WAL")
        return
    try:
        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        mode = row[0] if row else None
    except sqlite3.OperationalError:
        mode = None
    if mode is None or str(mode).lower() != "wal":
        conn.execute("PRAGMA journal_mode=DELETE")


@contextmanager
def transaction(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contractors(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 canonical_name TEXT NOT NULL UNIQUE,
 display_name TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contractor_aliases(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 contractor_id INTEGER NOT NULL REFERENCES contractors(id) ON DELETE CASCADE,
 alias_name TEXT NOT NULL,
 normalized_alias TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS tenders(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 external_key TEXT NOT NULL UNIQUE,
 tender_id TEXT,
 nit_rfp_no TEXT,
 work_name TEXT,
 publishing_office TEXT,
 region TEXT,
 zone_circle TEXT,
 division TEXT,
 subdivision TEXT,
 location TEXT,
 estimated_cost REAL,
 emd_amount REAL,
 submission_closing_datetime TEXT,
 bid_opening_datetime TEXT,
 awarded_contractor_id INTEGER REFERENCES contractors(id),
 quoted_value REAL,
 variance_percent REAL,
 bid_position TEXT NOT NULL DEFAULT 'Not Available',
 status TEXT,
 work_type TEXT,
 source_file TEXT,
 source_file_hash TEXT,
 first_imported_at TEXT NOT NULL,
 last_updated_at TEXT NOT NULL,
 manually_verified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS import_history(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 filename TEXT NOT NULL,
 file_hash TEXT NOT NULL,
 division TEXT,
 imported_at TEXT NOT NULL,
 total_rows INTEGER NOT NULL,
 inserted_rows INTEGER NOT NULL,
 updated_rows INTEGER NOT NULL,
 unchanged_rows INTEGER NOT NULL,
 rejected_rows INTEGER NOT NULL,
 error_log TEXT
);
CREATE TABLE IF NOT EXISTS tender_change_history(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 tender_id INTEGER NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
 changed_at TEXT NOT NULL,
 change_source TEXT NOT NULL,
 before_json TEXT,
 after_json TEXT
);
CREATE TABLE IF NOT EXISTS tender_analyses(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 title TEXT NOT NULL,
 source_filename TEXT,
 nit_no TEXT,
 work_name TEXT,
 zone_circle TEXT,
 division TEXT,
 location TEXT,
 estimated_cost REAL,
 work_type TEXT,
 bid_opening_date TEXT,
 extraction_json TEXT,
 result_json TEXT,
 costing_json TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 last_run_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tenders_tender_id ON tenders(tender_id);
CREATE INDEX IF NOT EXISTS idx_tenders_division ON tenders(division);
CREATE INDEX IF NOT EXISTS idx_tenders_contractor ON tenders(awarded_contractor_id);
CREATE INDEX IF NOT EXISTS idx_tenders_opening ON tenders(bid_opening_datetime);
CREATE INDEX IF NOT EXISTS idx_tenders_work_type ON tenders(work_type);
CREATE INDEX IF NOT EXISTS idx_alias_contractor ON contractor_aliases(contractor_id);
CREATE INDEX IF NOT EXISTS idx_tender_analyses_updated ON tender_analyses(updated_at);
CREATE INDEX IF NOT EXISTS idx_tender_analyses_nit ON tender_analyses(nit_no);
"""


def initialize_database(db_path: str | Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(1, ?)", (utcnow(),))
        migration_needed = conn.execute("SELECT 1 FROM schema_version WHERE version=2").fetchone() is None
        existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(tenders)")}
        for column in ("region", "zone_circle", "subdivision"):
            if column not in existing_columns:
                conn.execute(f"ALTER TABLE tenders ADD COLUMN {column} TEXT")
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_tenders_{column} ON tenders({column})")
        if migration_needed:
            rows = conn.execute("SELECT id, publishing_office FROM tenders WHERE publishing_office IS NOT NULL").fetchall()
            for row in rows:
                office = parse_publishing_office(row["publishing_office"])
                conn.execute(
                    """UPDATE tenders SET
                       region=COALESCE(region, ?), zone_circle=COALESCE(zone_circle, ?),
                       division=COALESCE(division, ?), subdivision=COALESCE(subdivision, ?)
                       WHERE id=?""",
                    (office["region"], office["zone_circle"], office["division"], office["subdivision"], row["id"]),
                )
        conn.execute("INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(2, ?)", (utcnow(),))
        hierarchy_fix_needed = conn.execute("SELECT 1 FROM schema_version WHERE version=3").fetchone() is None
        if hierarchy_fix_needed:
            rows = conn.execute("SELECT id, publishing_office FROM tenders WHERE publishing_office IS NOT NULL").fetchall()
            for row in rows:
                office = parse_publishing_office(row["publishing_office"])
                conn.execute(
                    "UPDATE tenders SET region=?, zone_circle=?, division=?, subdivision=? WHERE id=?",
                    (office["region"], office["zone_circle"], office["division"], office["subdivision"], row["id"]),
                )
        conn.execute("INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(3, ?)", (utcnow(),))
        conn.execute("INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(4, ?)", (utcnow(),))
        saved_title_fix_needed = conn.execute("SELECT 1 FROM schema_version WHERE version=5").fetchone() is None
        if saved_title_fix_needed:
            conn.execute(
                "UPDATE tender_analyses SET title=work_name WHERE work_name IS NOT NULL AND TRIM(work_name)<>''"
            )
        conn.execute("INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(5, ?)", (utcnow(),))
        analysis_columns = {row[1] for row in conn.execute("PRAGMA table_info(tender_analyses)")}
        if "costing_json" not in analysis_columns:
            conn.execute("ALTER TABLE tender_analyses ADD COLUMN costing_json TEXT")
        conn.execute("INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(6, ?)", (utcnow(),))


def restore_database_bytes(data: bytes, db_path: str | Path) -> None:
    """Integrity-check SQLite bytes and atomically replace a session database."""
    # Local import keeps the core database module independent of Streamlit during
    # normal query and migration operations.
    from .browser_sync import BrowserDatabaseError, validate_database_bytes

    validate_database_bytes(data)
    destination = Path(db_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_path = destination.with_suffix(".restore.db")
    staging_path.write_bytes(data)
    try:
        with sqlite3.connect(staging_path) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                raise BrowserDatabaseError("The selected database failed SQLite's integrity check.")
        initialize_database(staging_path)
        with sqlite3.connect(staging_path) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # Old WAL sidecars belong to the database being replaced and must never
        # be replayed into the restored main file.
        for suffix in ("-wal", "-shm"):
            Path(f"{destination}{suffix}").unlink(missing_ok=True)
        os.replace(staging_path, destination)
        initialize_database(destination)
    finally:
        staging_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{staging_path}{suffix}").unlink(missing_ok=True)


def reset_database(db_path: str | Path) -> None:
    """Atomically replace a database and its WAL sidecars with a clean schema."""
    destination = Path(db_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_path = destination.with_suffix(".reset.db")
    try:
        staging_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{staging_path}{suffix}").unlink(missing_ok=True)
        initialize_database(staging_path)
        with sqlite3.connect(staging_path) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # Never allow sidecars from the old database to be replayed into the new file.
        for suffix in ("-wal", "-shm"):
            Path(f"{destination}{suffix}").unlink(missing_ok=True)
        os.replace(staging_path, destination)
        initialize_database(destination)
    finally:
        staging_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{staging_path}{suffix}").unlink(missing_ok=True)


def resolve_contractor(conn: sqlite3.Connection, name: str | None, create: bool = True) -> int | None:
    normalized = normalize_contractor_name(name)
    if not normalized:
        return None
    row = conn.execute(
        "SELECT contractor_id FROM contractor_aliases WHERE normalized_alias=?", (normalized,)
    ).fetchone()
    if row:
        return int(row[0])
    if not create:
        return None
    now = utcnow()
    display = preferred_display_name(name) or normalized.title()
    cursor = conn.execute(
        "INSERT INTO contractors(canonical_name, display_name, created_at, updated_at) VALUES(?,?,?,?)",
        (normalized, display, now, now),
    )
    contractor_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO contractor_aliases(contractor_id, alias_name, normalized_alias) VALUES(?,?,?)",
        (contractor_id, str(name).strip(), normalized),
    )
    return contractor_id


def add_alias(conn: sqlite3.Connection, contractor_id: int, alias: str) -> None:
    normalized = normalize_contractor_name(alias)
    if not normalized:
        raise ValueError("Alias cannot be blank")
    existing = conn.execute(
        "SELECT contractor_id FROM contractor_aliases WHERE normalized_alias=?", (normalized,)
    ).fetchone()
    if existing and int(existing[0]) != int(contractor_id):
        raise ValueError("This alias already belongs to another contractor. Reassign it explicitly instead.")
    conn.execute(
        "INSERT OR IGNORE INTO contractor_aliases(contractor_id, alias_name, normalized_alias) VALUES(?,?,?)",
        (contractor_id, alias.strip(), normalized),
    )


def reassign_alias(conn: sqlite3.Connection, alias_id: int, contractor_id: int) -> None:
    conn.execute("UPDATE contractor_aliases SET contractor_id=? WHERE id=?", (contractor_id, alias_id))


def merge_contractors(conn: sqlite3.Connection, source_id: int, target_id: int) -> None:
    if source_id == target_id:
        raise ValueError("Choose two different contractors")
    source = conn.execute("SELECT id FROM contractors WHERE id=?", (source_id,)).fetchone()
    target = conn.execute("SELECT id FROM contractors WHERE id=?", (target_id,)).fetchone()
    if not source or not target:
        raise ValueError("Contractor not found")
    conn.execute("UPDATE tenders SET awarded_contractor_id=? WHERE awarded_contractor_id=?", (target_id, source_id))
    conn.execute("UPDATE contractor_aliases SET contractor_id=? WHERE contractor_id=?", (target_id, source_id))
    conn.execute("DELETE FROM contractors WHERE id=?", (source_id,))
    conn.execute("UPDATE contractors SET updated_at=? WHERE id=?", (utcnow(), target_id))


def record_change(conn: sqlite3.Connection, tender_id: int, source: str, before: dict[str, Any] | None, after: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO tender_change_history(tender_id, changed_at, change_source, before_json, after_json) VALUES(?,?,?,?,?)",
        (tender_id, utcnow(), source, json.dumps(before, default=str) if before else None, json.dumps(after, default=str)),
    )
