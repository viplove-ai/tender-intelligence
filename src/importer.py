from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, BinaryIO

import pandas as pd

from .classifier import classify_work_type
from .cleaning import calculate_variance, clean_text, normalize_key, parse_currency, parse_date, parse_publishing_office
from .database import connect, get_default_db_path, initialize_database, record_change, resolve_contractor, transaction, utcnow


MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_XLSX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_XLSX_MEMBERS = 2_000
MAX_REPORT_ROWS = 100_000
MAX_REPORT_COLUMNS = 250
FIELD_ALIASES = {
    "tender_id": ("tender id", "tenderid"),
    "nit_rfp_no": ("nit/rfp no", "nit rfp no", "nit no", "rfp no", "nit/ rfp no"),
    "work_name": ("name of work / subwork / packages", "name of work", "work name", "work description"),
    "publishing_office": ("tender publishing office", "publishing office", "office"),
    "region": ("region", "region name"),
    "zone_circle": ("zone/circle", "zone circle", "zone", "circle"),
    "division": ("division", "division name"),
    "subdivision": ("sub-division", "sub division", "subdivision"),
    "location": ("location", "place", "district"),
    "estimated_cost": ("estimated cost(inr)", "estimated cost", "estimated amount", "estimated cost inr"),
    "emd_amount": ("emd amount", "emd", "earnest money"),
    "submission_closing_datetime": ("bid submission closing date & time", "submission closing date", "bid closing date"),
    "bid_opening_datetime": ("bid opening date & time", "bid opening date", "opening date"),
    "contractor_name": ("awarded company name", "awarded contractor", "contractor name", "company name"),
    "quoted_value": ("quoted value", "awarded value", "contract value", "quoted amount"),
    "status": ("status", "tender status"),
}
ESSENTIAL_GROUPS = [("work_name",), ("tender_id", "nit_rfp_no")]


@dataclass
class ImportPreview:
    filename: str
    file_hash: str
    division: str | None
    records: list[dict[str, Any]]
    rejected: list[dict[str, Any]] = dataclass_field(default_factory=list)
    validation_errors: list[str] = dataclass_field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        actions = [row.get("_action") for row in self.records]
        return {
            "total_rows": len(self.records) + len(self.rejected),
            "new_tenders": actions.count("insert"),
            "updated_tenders": actions.count("update"),
            "unchanged_duplicates": actions.count("unchanged"),
            "rejected_rows": len(self.rejected),
            "awarded_records": sum(1 for row in self.records if row.get("_awarded")),
            "missing_award_warnings": sum(1 for row in self.records if row.get("_warning")),
        }


def _read_bytes(source: bytes | BinaryIO) -> bytes:
    if isinstance(source, bytes):
        return source
    pos = source.tell() if hasattr(source, "tell") else None
    data = source.read()
    if pos is not None:
        source.seek(pos)
    return data


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def match_columns(columns: list[Any]) -> dict[str, Any]:
    normalized = {normalize_key(column): column for column in columns}
    mapping: dict[str, Any] = {}
    for field, aliases in FIELD_ALIASES.items():
        candidates = [normalize_key(alias) for alias in aliases]
        exact = next((normalized[c] for c in candidates if c in normalized), None)
        if exact is not None:
            mapping[field] = exact
            continue
        for key, original in normalized.items():
            if any(candidate in key or key in candidate for candidate in candidates if len(key) > 3):
                mapping[field] = original
                break
    return mapping


def read_report(data: bytes, filename: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    suffix = Path(filename).suffix.lower()
    if suffix not in {".xls", ".xlsx"}:
        raise ValueError("Only .xls and .xlsx files are supported")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("File is larger than the 5 MB safety limit")
    if suffix == ".xlsx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                members = archive.infolist()
                if len(members) > MAX_XLSX_MEMBERS:
                    raise ValueError("The workbook contains too many embedded files")
                if sum(member.file_size for member in members) > MAX_XLSX_UNCOMPRESSED_BYTES:
                    raise ValueError("The workbook expands beyond the 64 MB safety limit")
        except zipfile.BadZipFile as exc:
            raise ValueError("The .xlsx file is not a valid Excel workbook") from exc
    engine = "xlrd" if suffix == ".xls" else "openpyxl"
    raw = pd.read_excel(io.BytesIO(data), header=None, engine=engine, dtype=object)
    if raw.shape[0] > MAX_REPORT_ROWS or raw.shape[1] > MAX_REPORT_COLUMNS:
        raise ValueError("The report exceeds the 100,000-row or 250-column safety limit")
    best_row, best_score = 0, -1
    for index in range(min(20, len(raw))):
        mapping = match_columns(raw.iloc[index].tolist())
        if len(mapping) > best_score:
            best_row, best_score = index, len(mapping)
    if best_score < 3:
        raise ValueError("Could not find a recognizable column header row in the first 20 rows")
    df = pd.read_excel(io.BytesIO(data), header=best_row, engine=engine, dtype=object)
    if df.shape[0] > MAX_REPORT_ROWS or df.shape[1] > MAX_REPORT_COLUMNS:
        raise ValueError("The report exceeds the 100,000-row or 250-column safety limit")
    return df.dropna(how="all"), match_columns(df.columns.tolist())


def detect_division(df: pd.DataFrame, mapping: dict[str, Any], override: str | None = None) -> str | None:
    if clean_text(override):
        return clean_text(override)
    for field in ("division", "publishing_office"):
        column = mapping.get(field)
        if column is not None:
            values = [clean_text(v) for v in df[column].tolist() if clean_text(v)]
            if values:
                common = pd.Series(values).mode().iloc[0]
                return parse_publishing_office(common)["division"] if field == "publishing_office" else common
    return None


def _external_key(record: dict[str, Any], reused: bool = False) -> str:
    tender_id = normalize_key(record.get("tender_id"))
    identity = "|".join([
        normalize_key(record.get("nit_rfp_no")), normalize_key(record.get("division")),
        normalize_key((record.get("bid_opening_datetime") or "")[:10]), normalize_key(record.get("work_name")),
    ])
    digest = hashlib.sha256(identity.encode()).hexdigest()[:20]
    if tender_id and not reused:
        return f"tid:{tender_id}"
    if tender_id:
        return f"tid:{tender_id}:reuse:{digest}"
    return f"fallback:{digest}"


def _compatible(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    comparisons = (("division", False), ("nit_rfp_no", False), ("bid_opening_datetime", True))
    conflicts = 0
    for field, date_only in comparisons:
        left, right = clean_text(existing.get(field)), clean_text(incoming.get(field))
        if date_only:
            left, right = (left or "")[:10], (right or "")[:10]
        if left and right and normalize_key(left) != normalize_key(right):
            conflicts += 1
    return conflicts < 2


def _find_existing(conn: Any, record: dict[str, Any]) -> dict[str, Any] | None:
    tender_id = clean_text(record.get("tender_id"))
    if tender_id:
        rows = conn.execute("SELECT * FROM tenders WHERE tender_id=? ORDER BY id", (tender_id,)).fetchall()
        compatible = [dict(row) for row in rows if _compatible(dict(row), record)]
        if compatible:
            return compatible[0]
    key = _external_key(record, reused=bool(tender_id))
    row = conn.execute("SELECT * FROM tenders WHERE external_key=?", (key,)).fetchone()
    return dict(row) if row else None


PERSISTED_FIELDS = [
    "tender_id", "nit_rfp_no", "work_name", "publishing_office", "region", "zone_circle", "division", "subdivision", "location",
    "estimated_cost", "emd_amount", "submission_closing_datetime", "bid_opening_datetime",
    "quoted_value", "variance_percent", "bid_position", "status", "work_type", "source_file", "source_file_hash",
]


def _merged(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for field in PERSISTED_FIELDS:
        value = incoming.get(field)
        if value is not None and clean_text(value) is not None:
            merged[field] = value
    if incoming.get("_awarded"):
        merged["contractor_name"] = incoming.get("contractor_name")
    return merged


def _changed(existing: dict[str, Any], merged: dict[str, Any]) -> bool:
    fields = PERSISTED_FIELDS[:-2] + ["contractor_name"]
    return any(existing.get(field) != merged.get(field) for field in fields if field in merged)


def prepare_import(
    source: bytes | BinaryIO,
    filename: str,
    division_override: str | None = None,
    db_path: str | Path | None = None,
) -> ImportPreview:
    initialize_database(db_path)
    data = _read_bytes(source)
    df, mapping = read_report(data, filename)
    missing = [" or ".join(group) for group in ESSENTIAL_GROUPS if not any(name in mapping for name in group)]
    division = detect_division(df, mapping, division_override)
    preview = ImportPreview(filename, file_sha256(data), division, [], validation_errors=[])
    if missing:
        preview.validation_errors.append("Missing essential columns: " + ", ".join(missing))
        return preview
    conn = connect(db_path)
    try:
        for row_number, (_, raw) in enumerate(df.iterrows(), start=1):
            def get(name: str) -> Any:
                return raw.get(mapping[name]) if name in mapping else None

            publishing_office = clean_text(get("publishing_office"))
            office = parse_publishing_office(publishing_office)
            record = {
                "tender_id": clean_text(get("tender_id")),
                "nit_rfp_no": clean_text(get("nit_rfp_no")),
                "work_name": clean_text(get("work_name")),
                "publishing_office": publishing_office,
                "region": clean_text(get("region")) or office["region"],
                "zone_circle": clean_text(get("zone_circle")) or office["zone_circle"],
                "division": clean_text(get("division")) or clean_text(division_override) or office["division"] or division,
                "subdivision": clean_text(get("subdivision")) or office["subdivision"],
                "location": clean_text(get("location")),
                "estimated_cost": parse_currency(get("estimated_cost")),
                "emd_amount": parse_currency(get("emd_amount")),
                "submission_closing_datetime": parse_date(get("submission_closing_datetime")),
                "bid_opening_datetime": parse_date(get("bid_opening_datetime")),
                "contractor_name": clean_text(get("contractor_name")),
                "quoted_value": parse_currency(get("quoted_value")),
                "status": clean_text(get("status")),
                "source_file": filename,
                "source_file_hash": preview.file_hash,
                "_row_number": row_number,
            }
            if not (record["tender_id"] or record["nit_rfp_no"]):
                preview.rejected.append({"row": row_number, "reason": "Missing Tender ID/NIT number"})
                continue
            existing = _find_existing(conn, record)
            if not record["work_name"] and not existing:
                preview.rejected.append({"row": row_number, "reason": "Missing work name for a new tender"})
                continue
            record["work_type"] = classify_work_type(record["work_name"]) if record["work_name"] else None
            record["variance_percent"], record["bid_position"] = calculate_variance(record["estimated_cost"], record["quoted_value"])
            record["_awarded"] = bool(record["contractor_name"] and (record["quoted_value"] or 0) > 0)
            record["_warning"] = None
            if record["contractor_name"] and not record["_awarded"]:
                record["_warning"] = "Contractor present but quoted value is missing/non-positive"
            elif (record["quoted_value"] or 0) > 0 and not record["contractor_name"]:
                record["_warning"] = "Positive quoted value present but contractor is missing"
            if existing:
                contractor = conn.execute("SELECT display_name FROM contractors WHERE id=?", (existing.get("awarded_contractor_id"),)).fetchone()
                existing["contractor_name"] = contractor[0] if contractor else None
                record["_existing_id"] = existing["id"]
                record["external_key"] = existing["external_key"]
                merged = _merged(existing, record)
                record["_action"] = "update" if _changed(existing, merged) else "unchanged"
            else:
                same_id = bool(record["tender_id"] and conn.execute("SELECT 1 FROM tenders WHERE tender_id=?", (record["tender_id"],)).fetchone())
                record["external_key"] = _external_key(record, reused=same_id)
                record["_action"] = "insert"
            preview.records.append(record)
    finally:
        conn.close()
    return preview


def _safe_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if not key.startswith("_") and key != "contractor_name"}


def _commit_preview(conn: Any, preview: ImportPreview) -> dict[str, int]:
    if preview.validation_errors:
        raise ValueError("; ".join(preview.validation_errors))
    counts = {"inserted_rows": 0, "updated_rows": 0, "unchanged_rows": 0, "rejected_rows": len(preview.rejected)}
    now = utcnow()
    for record in preview.records:
        action = record["_action"]
        if action == "unchanged":
            counts["unchanged_rows"] += 1
            continue
        contractor_id = resolve_contractor(conn, record.get("contractor_name")) if record.get("_awarded") else None
        clean = _safe_record(record)
        if action == "insert":
            fields = PERSISTED_FIELDS + ["external_key", "awarded_contractor_id", "first_imported_at", "last_updated_at", "manually_verified"]
            values = [clean.get(f) for f in PERSISTED_FIELDS] + [record["external_key"], contractor_id, now, now, 0]
            # Identifiers are application constants; imported values are bound parameters.
            cursor = conn.execute(
                f"INSERT INTO tenders({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",  # nosec B608
                values,
            )
            tender_pk = int(cursor.lastrowid)
            record_change(conn, tender_pk, f"Import: {preview.filename}", None, clean)
            counts["inserted_rows"] += 1
        else:
            old_row = dict(conn.execute("SELECT * FROM tenders WHERE id=?", (record["_existing_id"],)).fetchone())
            merged = _merged(old_row, record)
            if contractor_id is not None:
                merged["awarded_contractor_id"] = contractor_id
            update_fields = PERSISTED_FIELDS + ["awarded_contractor_id", "last_updated_at"]
            values = [merged.get(f) for f in PERSISTED_FIELDS] + [merged.get("awarded_contractor_id"), now, record["_existing_id"]]
            # Identifiers are application constants; imported values are bound parameters.
            conn.execute(
                f"UPDATE tenders SET {','.join(f'{f}=?' for f in update_fields)} WHERE id=?",  # nosec B608
                values,
            )
            record_change(conn, record["_existing_id"], f"Import: {preview.filename}", old_row, merged)
            counts["updated_rows"] += 1
    conn.execute(
        """INSERT INTO import_history(filename,file_hash,division,imported_at,total_rows,inserted_rows,updated_rows,unchanged_rows,rejected_rows,error_log)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (preview.filename, preview.file_hash, preview.division, now, preview.counts["total_rows"], counts["inserted_rows"],
         counts["updated_rows"], counts["unchanged_rows"], counts["rejected_rows"], json.dumps(preview.rejected) if preview.rejected else None),
    )
    return counts


def commit_import(preview: ImportPreview, db_path: str | Path | None = None, original_bytes: bytes | None = None) -> dict[str, int]:
    # `original_bytes` is accepted for API compatibility but no longer archived to server
    # disk: on Streamlit Community Cloud that disk is ephemeral and shared across sessions,
    # and the browser-synced SQLite file is the only durable copy of a user's data.
    with transaction(db_path) as conn:
        counts = _commit_preview(conn, preview)
    return counts


def commit_import_batch(items: list[tuple[ImportPreview, bytes]], db_path: str | Path | None = None) -> list[dict[str, int]]:
    with transaction(db_path) as conn:
        results = [_commit_preview(conn, preview) for preview, _ in items]
    return results


def prepare_import_batch(
    files: list[tuple[bytes, str]], division_override: str | None = None, db_path: str | Path | None = None
) -> list[tuple[ImportPreview, bytes]]:
    """Preview files in order against an isolated database so cross-file duplicates are accurate."""
    resolved_db_path = Path(db_path) if db_path is not None else get_default_db_path()
    initialize_database(resolved_db_path)
    with tempfile.TemporaryDirectory() as directory:
        staging = Path(directory) / "preview.db"
        with sqlite3.connect(resolved_db_path) as source, sqlite3.connect(staging) as destination:
            source.backup(destination)
        items = []
        for data, filename in files:
            preview = prepare_import(data, filename, division_override, staging)
            items.append((preview, data))
            if not preview.validation_errors:
                commit_import(preview, staging)
        return items
