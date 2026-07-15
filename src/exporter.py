from __future__ import annotations

import io
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .analytics import contractor_summary, dashboard_metrics, sort_tender_details_newest_first
from .database import get_default_db_path


UNTRUSTED_PREFIXES = ("=", "+", "-", "@")


def sanitize_excel_value(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip().startswith(UNTRUSTED_PREFIXES):
        return "'" + value
    return value


def sanitize_frame(df: pd.DataFrame) -> pd.DataFrame:
    clean = df.copy()
    for column in clean.select_dtypes(include=["object", "string"]).columns:
        clean[column] = clean[column].map(sanitize_excel_value)
    return clean


def build_excel_export(df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    ordered_tenders = sort_tender_details_newest_first(df)
    awards = ordered_tenders[ordered_tenders["is_awarded"]].copy() if not ordered_tenders.empty else ordered_tenders.copy()
    metrics = dashboard_metrics(df)
    dashboard = pd.DataFrame([{"Metric": key.replace("_", " ").title(), "Value": value} for key, value in metrics.items()])
    contractors = contractor_summary(df)
    pattern = (
        pd.pivot_table(awards, index="contractor_name", columns="work_type", values="id", aggfunc="count", fill_value=0)
        .reset_index() if not awards.empty else pd.DataFrame()
    )
    work_summary = (
        awards.groupby("work_type", dropna=False).agg(Awards=("id", "count"), Awarded_Value=("quoted_value", "sum")).reset_index()
        if not awards.empty else pd.DataFrame()
    )
    sheets = {
        "Dashboard Summary": dashboard,
        "Contractor Summary": contractors,
        "Award Details": awards,
        "Contractor Work Pattern": pattern,
        "Work Type Summary": work_summary,
        "All Filtered Tenders": ordered_tenders,
    }
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            safe = sanitize_frame(frame)
            safe.to_excel(writer, sheet_name=name[:31], index=False)
            worksheet = writer.book[name[:31]]
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions
            for cells in worksheet.columns:
                letter = cells[0].column_letter
                width = min(55, max(12, max(len(str(cell.value or "")) for cell in cells) + 2))
                worksheet.column_dimensions[letter].width = width
    return output.getvalue()


def create_backup(db_path: str | Path | None = None) -> Path:
    source = Path(db_path) if db_path is not None else get_default_db_path()
    if not source.exists():
        raise FileNotFoundError("Database has not been created yet")
    backup_dir = source.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"tender_intelligence_{datetime.now():%Y%m%d_%H%M%S}.db"
    with sqlite3.connect(source) as source_conn, sqlite3.connect(destination) as destination_conn:
        source_conn.backup(destination_conn)
    return destination


def build_database_backup(db_path: str | Path | None = None) -> bytes:
    source = Path(db_path) if db_path is not None else get_default_db_path()
    if not source.exists():
        raise FileNotFoundError("Database has not been created yet")
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "backup.db"
        with sqlite3.connect(source) as source_conn, sqlite3.connect(destination) as destination_conn:
            source_conn.backup(destination_conn)
        return destination.read_bytes()
