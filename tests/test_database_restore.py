import sqlite3

import pytest

from src.browser_sync import BrowserDatabaseError
from src.database import initialize_database, reset_database, restore_database_bytes
from src.exporter import build_database_backup


def test_restore_database_bytes_replaces_existing_database(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    initialize_database(source)
    initialize_database(destination)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES(99, 'source-marker')"
        )
    backup = build_database_backup(source)

    restore_database_bytes(backup, destination)

    with sqlite3.connect(destination) as connection:
        marker = connection.execute(
            "SELECT applied_at FROM schema_version WHERE version=99"
        ).fetchone()
    assert marker == ("source-marker",)


def test_restore_database_bytes_rejects_non_sqlite_data(tmp_path):
    with pytest.raises(BrowserDatabaseError):
        restore_database_bytes(b"not a database", tmp_path / "destination.db")


def test_reset_database_replaces_existing_data_with_clean_schema(tmp_path):
    database = tmp_path / "session.db"
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES(99, 'old-data-marker')"
        )

    reset_database(database)

    with sqlite3.connect(database) as connection:
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_version")}
        tender_count = connection.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
        analysis_count = connection.execute("SELECT COUNT(*) FROM tender_analyses").fetchone()[0]
    assert versions == {1, 2, 3, 4, 5, 6}
    assert tender_count == 0
    assert analysis_count == 0
